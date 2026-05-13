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

    Mirrors the Admin API connector's output shape (T3.2): a
    `metadata.results` JSONB array, each element a single bucket
    breakdown. ``occurred_at`` is set to midnight UTC of
    ``bucket_date`` — matches the connector's normalization.
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
    external_id = (
        f"usage:{occurred.isoformat()}:{next_day.isoformat()}"
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
