# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the T5.5.5 Usage list endpoint (Admin API daily aggregates).

Coverage: happy path, date-range filter, workspace + model filters,
totals math, RLS isolation, pagination cursor advances, empty state,
no-tenant-bound guard, invalid date range, multi-result-group records.

Like ``test_sessions_api.py``, this seeds synthetic ``telemetry_records``
via direct INSERT and exercises the route through FastAPI's TestClient.
RLS scoping is verified by seeding two tenants' rows and asserting the
requester only sees their own.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

os.environ.setdefault(
    "JWT_SIGNING_KEY",
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b",
)


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


@pytest.fixture
def clean_records() -> Iterator[None]:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE")
        )
    yield
    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE")
        )


def _bearer_for_tenant(tenant_id: str | None) -> dict[str, str]:
    from vargate_telemetry.auth.jwt import issue_session_jwt

    token = issue_session_jwt(
        user_id=str(uuid.uuid4()),
        email="probe@example.com",
        sso_provider="google",
        tenant_id=tenant_id,
    )
    return {"Authorization": f"Bearer {token}"}


def _seed_usage_record(
    tenant_id: str,
    *,
    bucket_date: date,
    results: list[dict],
) -> None:
    """Insert one ``record_type='usage'`` row with the given result-groups.

    Mirrors the Admin API connector's output shape: a
    `metadata.results` JSONB array, each element a single bucket
    breakdown. ``occurred_at`` is set to midnight UTC of
    ``bucket_date`` — matches the connector's normalization.

    T5.5.6 made external_id granular — ``usage:{start}:{end}:{model_or_-}:{workspace_or_-}``
    so multiple breakdown rows for the same date don't collide on
    the dedup UNIQUE. The helper mirrors that format using the first
    breakdown's model/workspace; tests that need to seed multiple
    breakdowns on the same date should call this helper once per
    breakdown with ``results=[single_dict]``.
    """
    from vargate_telemetry.db import engine

    occurred = datetime.combine(bucket_date, datetime.min.time(), tzinfo=timezone.utc)
    next_day = datetime.combine(
        date.fromordinal(bucket_date.toordinal() + 1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    md = {
        "starting_at": occurred.isoformat().replace("+00:00", "Z"),
        "ending_at": next_day.isoformat().replace("+00:00", "Z"),
        "results": results,
    }
    model_key = "-"
    workspace_key = "-"
    if results:
        first = results[0]
        model_key = first.get("model") or "-"
        workspace_key = first.get("workspace_id") or "-"
    external_id = (
        f"usage:{occurred.isoformat()}:{next_day.isoformat()}"
        f":{model_key}:{workspace_key}"
    )
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :tenant_id, 'usage', 'admin', :external_id,
                    :occurred_at, decode(:content_hash_hex, 'hex'),
                    :metadata,
                    (SELECT COALESCE(MAX(chain_seq), 0) + 1
                       FROM telemetry_records
                      WHERE tenant_id = :tenant_id_lookup),
                    decode(:prev_hex, 'hex'),
                    decode(:self_hex, 'hex')
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "tenant_id_lookup": tenant_id,
                "external_id": external_id,
                "occurred_at": occurred,
                "content_hash_hex": "00" * 32,
                "metadata": json.dumps(md),
                "prev_hex": "00" * 32,
                "self_hex": "11" * 32,
            },
        )


def _result_group(
    *,
    workspace_id: str | None = None,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
    web_searches: int = 0,
) -> dict:
    """Compact builder for a `metadata.results[i]` element."""
    return {
        "workspace_id": workspace_id,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "cache_creation": {
            "ephemeral_5m_input_tokens": cache_creation,
            "ephemeral_1h_input_tokens": 0,
        },
        "server_tool_use": {"web_search_requests": web_searches},
        "service_tier": None,
        "context_window": None,
        "account_id": None,
        "api_key_id": None,
        "inference_geo": None,
        "service_account_id": None,
    }


# ───────────────────────────────────────────────────────────────────────────
# Happy path
# ───────────────────────────────────────────────────────────────────────────


def test_list_usage_happy_path(
    clean_records: None, client: TestClient
) -> None:
    """Seed three days of usage → list returns three rows newest-first,
    totals reflect the sum."""
    tenant = "tnt_us_test_usage_happy"
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 11),
        results=[
            _result_group(
                input_tokens=1000,
                output_tokens=200,
                cache_read=500,
                cache_creation=100,
            )
        ],
    )
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 10),
        results=[
            _result_group(input_tokens=300, output_tokens=80)
        ],
    )
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 9),
        results=[
            _result_group(input_tokens=50, output_tokens=20)
        ],
    )

    r = client.get(
        "/usage?since=2026-05-09&until=2026-05-11",
        headers=_bearer_for_tenant(tenant),
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Three rows, newest-first.
    assert len(body["rows"]) == 3
    assert [row["date"] for row in body["rows"]] == [
        "2026-05-11",
        "2026-05-10",
        "2026-05-09",
    ]
    assert body["next_cursor"] is None

    # Totals are the sum across all returned rows.
    totals = body["totals"]
    assert totals["input_tokens"] == 1350
    assert totals["output_tokens"] == 300
    assert totals["cache_read_tokens"] == 500
    assert totals["cache_creation_tokens"] == 100
    assert totals["row_count"] == 3


def test_list_usage_expands_multi_result_record(
    clean_records: None, client: TestClient
) -> None:
    """A single record with three result groups yields three rows;
    totals sum across all groups."""
    tenant = "tnt_us_test_usage_multigroup"
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 11),
        results=[
            _result_group(
                workspace_id="ws_a", model="claude-sonnet-4-5",
                input_tokens=100, output_tokens=20,
            ),
            _result_group(
                workspace_id="ws_a", model="claude-opus-4-7",
                input_tokens=200, output_tokens=40,
            ),
            _result_group(
                workspace_id="ws_b", model="claude-sonnet-4-5",
                input_tokens=50, output_tokens=10,
            ),
        ],
    )

    r = client.get(
        "/usage?since=2026-05-11&until=2026-05-11",
        headers=_bearer_for_tenant(tenant),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["rows"]) == 3
    # All three rows share the same date but distinct (workspace, model).
    ws_model_pairs = {(row["workspace_id"], row["model"]) for row in body["rows"]}
    assert ws_model_pairs == {
        ("ws_a", "claude-sonnet-4-5"),
        ("ws_a", "claude-opus-4-7"),
        ("ws_b", "claude-sonnet-4-5"),
    }
    assert body["totals"]["input_tokens"] == 350
    assert body["totals"]["output_tokens"] == 70
    assert body["totals"]["row_count"] == 3


# ───────────────────────────────────────────────────────────────────────────
# Filters
# ───────────────────────────────────────────────────────────────────────────


def test_list_usage_workspace_filter(
    clean_records: None, client: TestClient
) -> None:
    tenant = "tnt_us_test_usage_ws_filter"
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 11),
        results=[
            _result_group(workspace_id="ws_a", input_tokens=100),
            _result_group(workspace_id="ws_b", input_tokens=200),
        ],
    )

    r = client.get(
        "/usage?since=2026-05-11&until=2026-05-11&workspace_id=ws_a",
        headers=_bearer_for_tenant(tenant),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["workspace_id"] == "ws_a"
    # Totals also respect the filter — ws_b is excluded from the sum.
    assert body["totals"]["input_tokens"] == 100


def test_list_usage_model_filter(
    clean_records: None, client: TestClient
) -> None:
    tenant = "tnt_us_test_usage_model_filter"
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 11),
        results=[
            _result_group(model="claude-sonnet-4-5", input_tokens=100),
            _result_group(model="claude-opus-4-7", input_tokens=200),
        ],
    )

    r = client.get(
        "/usage?since=2026-05-11&until=2026-05-11&model=claude-opus-4-7",
        headers=_bearer_for_tenant(tenant),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["model"] == "claude-opus-4-7"
    assert body["totals"]["input_tokens"] == 200


def test_list_usage_date_range_filter_excludes_out_of_window(
    clean_records: None, client: TestClient
) -> None:
    tenant = "tnt_us_test_usage_range"
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 5),
        results=[_result_group(input_tokens=100)],
    )
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 10),
        results=[_result_group(input_tokens=200)],
    )
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 15),
        results=[_result_group(input_tokens=300)],
    )

    r = client.get(
        "/usage?since=2026-05-08&until=2026-05-12",
        headers=_bearer_for_tenant(tenant),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["date"] == "2026-05-10"
    assert body["totals"]["input_tokens"] == 200


def test_list_usage_invalid_date_range_returns_400(
    clean_records: None, client: TestClient
) -> None:
    r = client.get(
        "/usage?since=2026-05-11&until=2026-05-09",
        headers=_bearer_for_tenant("tnt_us_test_dr"),
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "invalid_date_range"


# ───────────────────────────────────────────────────────────────────────────
# Pagination
# ───────────────────────────────────────────────────────────────────────────


def test_list_usage_cursor_advances(
    clean_records: None, client: TestClient
) -> None:
    """Seed 5 days, page size 2 → three pages: 2, 2, 1. Cursor walks
    newest-to-oldest. Totals remain constant across pages (the full
    filtered set, not per-page)."""
    tenant = "tnt_us_test_usage_page"
    for day in range(7, 12):  # 2026-05-07 .. 2026-05-11
        _seed_usage_record(
            tenant,
            bucket_date=date(2026, 5, day),
            results=[_result_group(input_tokens=day * 10)],
        )

    seen_dates: list[str] = []
    cursor: str | None = None
    page_count = 0
    while True:
        page_count += 1
        url = "/usage?since=2026-05-07&until=2026-05-11&limit=2"
        if cursor:
            url += f"&cursor={cursor}"
        r = client.get(url, headers=_bearer_for_tenant(tenant))
        assert r.status_code == 200, r.text
        body = r.json()
        seen_dates.extend(row["date"] for row in body["rows"])
        # Totals are the full-set aggregate — same on every page.
        assert body["totals"]["row_count"] == 5
        assert body["totals"]["input_tokens"] == (70 + 80 + 90 + 100 + 110)
        if not body["next_cursor"]:
            break
        cursor = body["next_cursor"]
        if page_count > 10:
            pytest.fail("cursor walk did not terminate")

    assert page_count == 3
    assert seen_dates == [
        "2026-05-11",
        "2026-05-10",
        "2026-05-09",
        "2026-05-08",
        "2026-05-07",
    ]


def test_list_usage_invalid_cursor_returns_400(
    clean_records: None, client: TestClient
) -> None:
    r = client.get(
        "/usage?cursor=not-base64!",
        headers=_bearer_for_tenant("tnt_us_test_bad_cursor"),
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "invalid_cursor"


# ───────────────────────────────────────────────────────────────────────────
# Empty state + auth guards
# ───────────────────────────────────────────────────────────────────────────


def test_list_usage_empty_state_for_tenant_with_no_records(
    clean_records: None, client: TestClient
) -> None:
    """Tenant exists but has zero Admin API records → 200 with empty
    rows + zero totals. Empty state is a legitimate response, not a
    404 — the dashboard renders the "no usage yet" copy."""
    tenant = "tnt_us_test_empty"
    r = client.get("/usage", headers=_bearer_for_tenant(tenant))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows"] == []
    assert body["totals"]["row_count"] == 0
    assert body["totals"]["input_tokens"] == 0
    assert body["next_cursor"] is None


def test_list_usage_no_tenant_bound_returns_400(
    clean_records: None, client: TestClient
) -> None:
    """JWT without a tenant binding → 400 ``no_tenant_bound``. Same
    failure mode as ``/sessions`` — the dashboard sends users back
    through onboarding when this fires."""
    r = client.get("/usage", headers=_bearer_for_tenant(None))
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "no_tenant_bound"


def test_list_usage_requires_auth(
    clean_records: None, client: TestClient
) -> None:
    r = client.get("/usage")
    assert r.status_code == 401, r.text


# ───────────────────────────────────────────────────────────────────────────
# RLS isolation
# ───────────────────────────────────────────────────────────────────────────


def test_list_usage_rls_isolates_tenants(
    clean_records: None, client: TestClient
) -> None:
    """Seed records for two tenants; each tenant's call returns only
    its own rows. The Postgres RLS policy on ``telemetry_records``
    prevents cross-tenant reads regardless of the SQL the route
    issues."""
    tenant_a = "tnt_us_test_usage_rls_a"
    tenant_b = "tnt_us_test_usage_rls_b"

    _seed_usage_record(
        tenant_a,
        bucket_date=date(2026, 5, 11),
        results=[_result_group(input_tokens=100)],
    )
    _seed_usage_record(
        tenant_b,
        bucket_date=date(2026, 5, 11),
        results=[_result_group(input_tokens=999_999)],
    )

    r_a = client.get(
        "/usage?since=2026-05-11&until=2026-05-11",
        headers=_bearer_for_tenant(tenant_a),
    )
    assert r_a.status_code == 200
    body_a = r_a.json()
    assert len(body_a["rows"]) == 1
    assert body_a["totals"]["input_tokens"] == 100  # not 999,999

    r_b = client.get(
        "/usage?since=2026-05-11&until=2026-05-11",
        headers=_bearer_for_tenant(tenant_b),
    )
    assert r_b.status_code == 200
    body_b = r_b.json()
    assert len(body_b["rows"]) == 1
    assert body_b["totals"]["input_tokens"] == 999_999


# ───────────────────────────────────────────────────────────────────────────
# Bucket-grain isolation: Sessions records do NOT leak into Usage and
# vice versa.
# ───────────────────────────────────────────────────────────────────────────


def test_list_usage_excludes_code_analytics_records(
    clean_records: None, client: TestClient
) -> None:
    """Seed a code_analytics record on the same date as a usage
    record. Usage endpoint must only return the admin record."""
    from vargate_telemetry.db import engine

    tenant = "tnt_us_test_usage_excludes_ca"
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 11),
        results=[_result_group(input_tokens=100)],
    )
    # Code analytics record on the same date.
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, 'code_analytics', 'code_analytics',
                    'code_analytics:2026-05-11:dev@example.com',
                    '2026-05-11T08:00:00+00:00',
                    decode('00' || repeat('0', 62), 'hex'),
                    :metadata,
                    (SELECT COALESCE(MAX(chain_seq), 0) + 1
                       FROM telemetry_records WHERE tenant_id = :t2),
                    decode('00' || repeat('0', 62), 'hex'),
                    decode('11' || repeat('1', 62), 'hex')
                )
                """
            ),
            {
                "t": tenant,
                "t2": tenant,
                "metadata": json.dumps(
                    {
                        "actor": {
                            "type": "user_actor",
                            "email_address": "dev@example.com",
                        },
                        "input_tokens": 9999,
                    }
                ),
            },
        )

    r = client.get(
        "/usage?since=2026-05-11&until=2026-05-11",
        headers=_bearer_for_tenant(tenant),
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["input_tokens"] == 100  # not 9999
    assert body["totals"]["input_tokens"] == 100


# ───────────────────────────────────────────────────────────────────────────
# T5.5.6: cost computation + workspace name resolution
# ───────────────────────────────────────────────────────────────────────────


def _seed_workspace(tenant_id: str, workspace_id: str, name: str) -> None:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO workspaces (tenant_id, workspace_id, name)
                VALUES (:t, :w, :n)
                ON CONFLICT (tenant_id, workspace_id)
                DO UPDATE SET name = EXCLUDED.name
                """
            ),
            {"t": tenant_id, "w": workspace_id, "n": name},
        )


@pytest.fixture
def clean_workspaces() -> Iterator[None]:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(sql_text("TRUNCATE TABLE workspaces"))
    yield
    with engine.begin() as conn:
        conn.execute(sql_text("TRUNCATE TABLE workspaces"))


def test_list_usage_populates_cost_for_known_model(
    clean_records: None,
    clean_workspaces: None,
    client: TestClient,
) -> None:
    """A Sonnet 4.5 breakdown row gets ``estimated_cost_usd`` filled in
    from the rate card; totals carry ``total_cost_usd`` over the
    aggregated set."""
    tenant = "tnt_us_test_usage_cost"
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 11),
        results=[
            _result_group(
                model="claude-sonnet-4-5-20250929",
                input_tokens=180_593,
                output_tokens=26_235,
                cache_read=689_700,
                cache_creation=0,
            )
        ],
    )

    r = client.get(
        "/usage?since=2026-05-11&until=2026-05-11",
        headers=_bearer_for_tenant(tenant),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    row = body["rows"][0]
    # Expected: input 0.541779 + output 0.393525 + cache_read 0.206910
    # = 1.142214. Pydantic serializes Decimal as a string.
    assert row["estimated_cost_usd"] == "1.142214"

    # Totals cost rounds to 2 decimals.
    assert body["totals"]["total_cost_usd"] == "1.14"
    assert body["totals"]["rows_without_cost"] == 0


def test_list_usage_null_model_leaves_cost_null(
    clean_records: None,
    clean_workspaces: None,
    client: TestClient,
) -> None:
    """Legacy aggregate rows (model=null) leave estimated_cost_usd as
    null and bump rows_without_cost. Historical records DON'T DISAPPEAR
    just because we can't compute their cost — the API still returns
    them."""
    tenant = "tnt_us_test_usage_null_model"
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 11),
        results=[_result_group(model=None, input_tokens=1000)],
    )

    r = client.get(
        "/usage?since=2026-05-11&until=2026-05-11",
        headers=_bearer_for_tenant(tenant),
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["estimated_cost_usd"] is None
    assert body["totals"]["total_cost_usd"] is None
    assert body["totals"]["rows_without_cost"] == 1


def test_list_usage_mixed_known_and_unknown_models(
    clean_records: None,
    clean_workspaces: None,
    client: TestClient,
) -> None:
    """Across DIFFERENT dates: one date has a costed (known-model) row,
    another has an unknown-model row. Totals reflect a FLOOR
    (rows_without_cost > 0). UI uses this signal to show '≥ $X' rather
    than '$X exactly'.

    (Mixing known + unknown on the SAME date is covered by the
    supersession test below — the legacy aggregate gets hidden when a
    breakdown for the same date exists. To exercise the rows_without_cost
    path we need a date that has ONLY the legacy aggregate, so we seed
    them on separate dates.)
    """
    tenant = "tnt_us_test_usage_mixed_cost"
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 11),
        results=[
            _result_group(
                model="claude-sonnet-4-5-20250929",
                input_tokens=1_000_000,  # → $3.00
                output_tokens=0,
                cache_read=0,
                cache_creation=0,
            ),
        ],
    )
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 10),
        results=[
            _result_group(
                model=None,
                input_tokens=999_999,
            ),
        ],
    )

    r = client.get(
        "/usage?since=2026-05-10&until=2026-05-11",
        headers=_bearer_for_tenant(tenant),
    )
    body = r.json()
    assert body["totals"]["total_cost_usd"] == "3.00"
    assert body["totals"]["rows_without_cost"] == 1


def test_list_usage_resolves_workspace_name(
    clean_records: None,
    clean_workspaces: None,
    client: TestClient,
) -> None:
    """``workspaces`` row populated → Usage row carries ``workspace_name``."""
    tenant = "tnt_us_test_usage_wsname"
    _seed_workspace(tenant, "wrkspc_a", "Engineering")
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 11),
        results=[
            _result_group(
                workspace_id="wrkspc_a",
                model="claude-sonnet-4-5-20250929",
                input_tokens=100,
            )
        ],
    )

    r = client.get(
        "/usage?since=2026-05-11&until=2026-05-11",
        headers=_bearer_for_tenant(tenant),
    )
    assert r.status_code == 200
    body = r.json()
    row = body["rows"][0]
    assert row["workspace_id"] == "wrkspc_a"
    assert row["workspace_name"] == "Engineering"


def test_list_usage_unresolved_workspace_returns_null_name(
    clean_records: None,
    clean_workspaces: None,
    client: TestClient,
) -> None:
    """Workspace ID present but no matching row in `workspaces` → name
    is null. UI falls back to rendering the raw ID."""
    tenant = "tnt_us_test_usage_unresolved"
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 11),
        results=[
            _result_group(
                workspace_id="wrkspc_unknown",
                model="claude-sonnet-4-5-20250929",
            )
        ],
    )

    r = client.get(
        "/usage?since=2026-05-11&until=2026-05-11",
        headers=_bearer_for_tenant(tenant),
    )
    body = r.json()
    row = body["rows"][0]
    assert row["workspace_id"] == "wrkspc_unknown"
    assert row["workspace_name"] is None


def test_list_usage_supersedes_legacy_aggregate_when_breakdown_present(
    clean_records: None,
    clean_workspaces: None,
    client: TestClient,
) -> None:
    """T5.5.6: a legacy aggregate record (model=null) on the same
    date as a per-model breakdown record is HIDDEN by the Usage API
    to avoid double-counting. The audit-chain rows stay in the DB —
    only the view filters them."""
    tenant = "tnt_us_test_supersession"
    # Legacy aggregate row for 2026-05-11 — model=null, total counts.
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 11),
        results=[
            _result_group(model=None, input_tokens=186_313, output_tokens=32_349)
        ],
    )
    # Per-model breakdown rows for the same day — these are what the
    # T5.5.6 connector emits.
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 11),
        results=[
            _result_group(
                model="claude-sonnet-4-5-20250929",
                input_tokens=354,
                output_tokens=13_411,
            )
        ],
    )
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 11),
        results=[
            _result_group(
                model="claude-haiku-4-5-20251001",
                input_tokens=185_959,
                output_tokens=18_938,
            )
        ],
    )
    # And a date with ONLY a legacy aggregate (no breakdown row).
    # That row must NOT be hidden — it's the only data for that day.
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 8),
        results=[_result_group(model=None, input_tokens=36, output_tokens=1738)],
    )

    r = client.get(
        "/usage?since=2026-05-08&until=2026-05-11",
        headers=_bearer_for_tenant(tenant),
    )
    assert r.status_code == 200, r.text
    body = r.json()

    models_by_date: dict[str, set] = {}
    for row in body["rows"]:
        models_by_date.setdefault(row["date"], set()).add(row["model"])

    # 2026-05-11: ONLY the two per-model rows. The legacy aggregate
    # is hidden.
    assert models_by_date.get("2026-05-11") == {
        "claude-sonnet-4-5-20250929",
        "claude-haiku-4-5-20251001",
    }, f"expected breakdown-only on 2026-05-11; got {models_by_date.get('2026-05-11')}"

    # 2026-05-08: only the legacy aggregate, so it survives.
    assert models_by_date.get("2026-05-08") == {None}

    # Totals reflect the dedup — should be (sonnet + haiku + legacy-08),
    # NOT (legacy-11 + sonnet + haiku + legacy-08).
    # legacy-11 was 186313, which equals sonnet 354 + haiku 185959 → if
    # the dedup is broken, totals doubles 2026-05-11's input.
    expected_input = 354 + 185_959 + 36  # 186,349
    assert body["totals"]["input_tokens"] == expected_input, (
        f"totals doubled — supersession filter failed; "
        f"got {body['totals']['input_tokens']} expected {expected_input}"
    )
    # row_count is the un-superseded set: 3 (2 per-model + 1 legacy-only).
    assert body["totals"]["row_count"] == 3
    # rows_without_cost == 1 (the surviving legacy-only row).
    assert body["totals"]["rows_without_cost"] == 1


def test_list_usage_cache_creation_reads_nested_if_flat_missing(
    clean_records: None,
    clean_workspaces: None,
    client: TestClient,
) -> None:
    """T5.5.6 group_by'd response drops the flat
    ``cache_creation_input_tokens`` field and only ships the nested
    ``cache_creation.{ephemeral_5m, ephemeral_1h}``. The Usage API
    falls back to the nested sum when the flat field is missing."""
    tenant = "tnt_us_test_usage_nested_cache"
    # Manually seed a record whose result has NO flat cache_creation
    # field — only the nested dict.
    from vargate_telemetry.db import engine

    metadata = {
        "starting_at": "2026-05-11T00:00:00Z",
        "ending_at": "2026-05-12T00:00:00Z",
        "results": [
            {
                "workspace_id": None,
                "model": "claude-sonnet-4-5-20250929",
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 50_000,
                    "ephemeral_1h_input_tokens": 25_000,
                },
                "server_tool_use": {"web_search_requests": 0},
                # NOTE: no cache_creation_input_tokens flat field
            }
        ],
    }
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, 'usage', 'admin',
                    'usage:2026-05-11T00:00:00Z:2026-05-12T00:00:00Z:claude-sonnet-4-5-20250929:-',
                    '2026-05-11T00:00:00+00:00',
                    decode('00' || repeat('0', 62), 'hex'),
                    :metadata,
                    (SELECT COALESCE(MAX(chain_seq), 0) + 1
                       FROM telemetry_records WHERE tenant_id = :t2),
                    decode('00' || repeat('0', 62), 'hex'),
                    decode('11' || repeat('1', 62), 'hex')
                )
                """
            ),
            {
                "t": tenant,
                "t2": tenant,
                "metadata": json.dumps(metadata),
            },
        )

    r = client.get(
        "/usage?since=2026-05-11&until=2026-05-11",
        headers=_bearer_for_tenant(tenant),
    )
    body = r.json()
    assert body["rows"][0]["cache_creation_tokens"] == 75_000  # 50k + 25k
    assert body["totals"]["cache_creation_tokens"] == 75_000


def test_list_usage_cache_creation_nullif_falls_through_when_flat_is_zero(
    clean_records: None,
    clean_workspaces: None,
    client: TestClient,
) -> None:
    """T5.5.7 regression pin: when the result blob has BOTH a flat
    ``cache_creation_input_tokens: 0`` (Pydantic default) AND nested
    real values, the SQL must use the nested sum. Without NULLIF the
    COALESCE picks the non-null 0 and reports 0 cache creation —
    silently understating cost and breaking the cache-efficiency
    chart.

    Real-data manifestation (founder's tnt_eu_38b3047725704cb1): the
    T5.5.6 connector + Pydantic UsageBreakdown serialized
    ``cache_creation_input_tokens: 0`` for every per-model row even
    when ``cache_creation.ephemeral_5m_input_tokens`` was 305_716.
    The bug hid ~$50 of real Sonnet cache-creation cost on Sera's
    tenant across 90 days.
    """
    from vargate_telemetry.db import engine

    tenant = "tnt_us_test_usage_cc_nullif"
    metadata = {
        "starting_at": "2026-05-11T00:00:00Z",
        "ending_at": "2026-05-12T00:00:00Z",
        "results": [
            {
                "workspace_id": None,
                "model": "claude-sonnet-4-5-20250929",
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                # Flat field present BUT zero (the Pydantic default
                # case that masks the nested value).
                "cache_creation_input_tokens": 0,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 305_716,
                    "ephemeral_1h_input_tokens": 0,
                },
                "server_tool_use": {"web_search_requests": 0},
            }
        ],
    }
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, 'usage', 'admin',
                    'usage:2026-05-11T00:00:00Z:2026-05-12T00:00:00Z:claude-sonnet-4-5-20250929:-',
                    '2026-05-11T00:00:00+00:00',
                    decode('00' || repeat('0', 62), 'hex'),
                    :metadata,
                    (SELECT COALESCE(MAX(chain_seq), 0) + 1
                       FROM telemetry_records WHERE tenant_id = :t2),
                    decode('00' || repeat('0', 62), 'hex'),
                    decode('11' || repeat('1', 62), 'hex')
                )
                """
            ),
            {
                "t": tenant,
                "t2": tenant,
                "metadata": json.dumps(metadata),
            },
        )

    r = client.get(
        "/usage?since=2026-05-11&until=2026-05-11",
        headers=_bearer_for_tenant(tenant),
    )
    body = r.json()
    # NULLIF makes the flat 0 fall through to the nested sum.
    assert body["rows"][0]["cache_creation_tokens"] == 305_716
    assert body["totals"]["cache_creation_tokens"] == 305_716


def test_list_usage_totals_cost_equals_sum_of_per_row_costs(
    clean_records: None,
    clean_workspaces: None,
    client: TestClient,
) -> None:
    """T5.5.7 regression pin: ``totals.total_cost_usd`` must equal
    the sum of every row's ``estimated_cost_usd`` (within
    rounding). The cost-by-model SQL and the per-row SQL share the
    same cache_creation NULLIF logic; a divergence between them
    means one SQL block is missing the fix and customers see
    inconsistent figures between the row column and the totals
    cell.

    Real-data manifestation: founder's tnt_eu_38b3047725704cb1
    sum-of-row-costs was $304.09 vs totals $236.59 because the
    cost-by-model SQL was missing NULLIF over the flat
    cache_creation_input_tokens, so Sonnet's ~$68 of cache-creation
    cost dropped out of the aggregate.
    """
    from decimal import Decimal

    tenant = "tnt_us_test_totals_match"
    # Sonnet row with real cache_creation in the nested dict +
    # flat 0 (the bug pattern that NULLIF unblocks).
    metadata = {
        "starting_at": "2026-05-11T00:00:00Z",
        "ending_at": "2026-05-12T00:00:00Z",
        "results": [
            {
                "workspace_id": None,
                "model": "claude-sonnet-4-5-20250929",
                "input_tokens": 1_000_000,
                "output_tokens": 100_000,
                "cache_read_input_tokens": 500_000,
                "cache_creation_input_tokens": 0,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 2_000_000,
                    "ephemeral_1h_input_tokens": 0,
                },
                "server_tool_use": {"web_search_requests": 0},
            }
        ],
    }
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, 'usage', 'admin',
                    'usage:2026-05-11T00:00:00Z:2026-05-12T00:00:00Z:claude-sonnet-4-5-20250929:-',
                    '2026-05-11T00:00:00+00:00',
                    decode('00' || repeat('0', 62), 'hex'),
                    :metadata,
                    (SELECT COALESCE(MAX(chain_seq), 0) + 1
                       FROM telemetry_records WHERE tenant_id = :t2),
                    decode('00' || repeat('0', 62), 'hex'),
                    decode('11' || repeat('1', 62), 'hex')
                )
                """
            ),
            {"t": tenant, "t2": tenant, "metadata": json.dumps(metadata)},
        )

    r = client.get(
        "/usage?since=2026-05-11&until=2026-05-11",
        headers=_bearer_for_tenant(tenant),
    )
    body = r.json()

    per_row_sum = Decimal("0")
    for row in body["rows"]:
        if row["estimated_cost_usd"]:
            per_row_sum += Decimal(row["estimated_cost_usd"])

    # The totals string is 2-decimal USD; round per_row_sum the
    # same way before comparing.
    rounded = per_row_sum.quantize(Decimal("0.01"))
    assert body["totals"]["total_cost_usd"] == str(rounded), (
        f"totals.total_cost_usd ({body['totals']['total_cost_usd']}) "
        f"must equal sum of per-row costs ({rounded})"
    )


def test_list_usage_limit_1000_accepted(
    clean_records: None,
    clean_workspaces: None,
    client: TestClient,
) -> None:
    """T5.5.7: chart fetches request limit=1000 so 30 days of
    per-model breakdown lands in a single round-trip. The cap was 200
    pre-T5.5.7; lifting it to 1000 is the no-parallel-chart-endpoint
    contract."""
    tenant = "tnt_us_test_usage_limit_1000"
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 11),
        results=[
            _result_group(
                model="claude-sonnet-4-5-20250929", input_tokens=100
            )
        ],
    )
    r = client.get(
        "/usage?since=2026-05-11&until=2026-05-11&limit=1000",
        headers=_bearer_for_tenant(tenant),
    )
    assert r.status_code == 200, r.text


def test_list_usage_limit_1001_rejected(
    clean_records: None,
    clean_workspaces: None,
    client: TestClient,
) -> None:
    """Above-cap limit returns 422 (FastAPI's standard validation
    response) so a runaway client can't drag back millions of rows."""
    r = client.get(
        "/usage?limit=1001",
        headers=_bearer_for_tenant("tnt_us_test_limit_cap"),
    )
    assert r.status_code == 422, r.text


# ───────────────────────────────────────────────────────────────────────────
# Cache-efficiency recommendations (TM5 T5.5)
# ───────────────────────────────────────────────────────────────────────────


def test_cache_recommendation_tiers() -> None:
    """The pure verdict function, tier by tier (no DB)."""
    from vargate_telemetry.api.usage import _cache_recommendation

    # Below the volume floor → ok, no nag.
    sev, _hit, _ = _cache_recommendation(1_000, 0, 0)
    assert sev == "ok"

    # High volume, no caching at all → warn, hit_rate None.
    sev, hit, text = _cache_recommendation(500_000, 0, 0)
    assert sev == "warn" and hit is None
    assert "No prompt caching" in text

    # Low reuse (<0.5) → warn.
    sev, hit, _ = _cache_recommendation(0, 100_000, 400_000)
    assert sev == "warn" and abs(hit - 0.2) < 1e-9

    # Moderate (0.5–0.8) → info.
    sev, hit, _ = _cache_recommendation(0, 600_000, 400_000)
    assert sev == "info" and abs(hit - 0.6) < 1e-9

    # Healthy (≥0.8) → ok.
    sev, hit, _ = _cache_recommendation(0, 900_000, 100_000)
    assert sev == "ok" and abs(hit - 0.9) < 1e-9


def test_cache_recommendations_endpoint(
    clean_records: None, client: TestClient
) -> None:
    tenant = "tnt_us_cache_recs"
    _seed_usage_record(
        tenant,
        bucket_date=date(2026, 5, 20),
        results=[
            _result_group(
                model="claude-opus-4-7",
                cache_read=900_000,
                cache_creation=100_000,
            ),
            _result_group(model="claude-haiku-4-5", input_tokens=500_000),
        ],
    )
    r = client.get(
        "/usage/cache-recommendations", headers=_bearer_for_tenant(tenant)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    by_model = {m["model"]: m for m in body["models"]}

    assert by_model["claude-opus-4-7"]["severity"] == "ok"
    assert abs(by_model["claude-opus-4-7"]["cache_hit_rate"] - 0.9) < 1e-9
    assert by_model["claude-haiku-4-5"]["severity"] == "warn"
    assert by_model["claude-haiku-4-5"]["cache_hit_rate"] is None
    # Warnings sort first (most actionable).
    assert body["models"][0]["severity"] == "warn"
    # Overall = read / (read + creation) = 900k / 1000k.
    assert abs(body["overall_hit_rate"] - 0.9) < 1e-9


def test_cache_recommendations_empty_window(
    clean_records: None, client: TestClient
) -> None:
    r = client.get(
        "/usage/cache-recommendations",
        headers=_bearer_for_tenant("tnt_us_cache_empty"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["models"] == []
    assert body["overall_hit_rate"] is None


def test_cache_recommendations_no_tenant_400(client: TestClient) -> None:
    r = client.get(
        "/usage/cache-recommendations", headers=_bearer_for_tenant(None)
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "no_tenant_bound"
