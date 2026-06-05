# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the TM8 OpenAI Admin costs pull (``pull_openai_costs``).

Costs are the authoritative billed-spend stream (recon §3): per
(line_item, project) grain, NO user_id, ``amount.value`` as a Decimal
(sometimes sci-notation on the wire). Mirrors the usage-pull test
structure:

  - happy path → cost rows land with the pinned external_id and the
    Decimal amount preserved exactly (string) in metadata;
  - sci-notation amount round-trips without float drift;
  - 403 → soft-skip dict;
  - dedup-only second run → cursor still advances;
  - empty bucket → sentinel, cursor advances.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Iterator

import httpx
import pytest
from sqlalchemy import text as sql_text

from vargate_telemetry.openai import OpenAIAdminClient

_START = int(datetime(2026, 5, 11, tzinfo=timezone.utc).timestamp())
_END = int(datetime(2026, 5, 12, tzinfo=timezone.utc).timestamp())


def _cost_result(
    *,
    value: float = 0.0002225,
    line_item: str = "gpt-4o-2024-08-06, input",
    project_id: str | None = "proj_alpha",
) -> dict:
    return {
        "object": "organization.costs.result",
        "amount": {"value": value, "currency": "usd"},
        "line_item": line_item,
        "quantity": 0.089,
        "project_id": project_id,
        "project_name": "Alpha",
        "organization_id": "org-XXXX",
        "organization_name": "Acme",
    }


def _bucket(results: list[dict]) -> dict:
    return {
        "object": "bucket",
        "start_time": _START,
        "end_time": _END,
        "results": results,
    }


def _page_handler(
    results: list[dict],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "page",
                "data": [_bucket(results)],
                "has_more": False,
                "next_page": None,
            },
        )

    return handler


def _stub_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> OpenAIAdminClient:
    return OpenAIAdminClient(
        api_key="sk-admin-test",
        base_url="https://api.test/v1/organization",
        min_wait=0.0,
        max_wait=0.0,
        wait_multiplier=0.0,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
def clean_pull_state() -> Iterator[None]:
    from vargate_telemetry.billing import set_dispatcher_for_test
    from vargate_telemetry.db import engine
    from vargate_telemetry.metering import _redis

    r = _redis()
    for key in r.scan_iter("vargate:meter:*"):
        r.delete(key)
    truncate_sql = (
        "TRUNCATE TABLE telemetry_records, usage_records, "
        "pull_state, tenants, billing_retry, tenant_billing "
        "RESTART IDENTITY CASCADE"
    )
    with engine.begin() as conn:
        conn.execute(sql_text(truncate_sql))
    set_dispatcher_for_test(None)
    yield
    for key in r.scan_iter("vargate:meter:*"):
        r.delete(key)
    with engine.begin() as conn:
        conn.execute(sql_text(truncate_sql))
    set_dispatcher_for_test(None)


def _provision_tenant(tenant_id: str) -> None:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "billing_status) VALUES (:t, 'us', TRUE, 'trial') "
                "ON CONFLICT (tenant_id) DO NOTHING"
            ),
            {"t": tenant_id},
        )


# ───────────────────────────────────────────────────────────────────────────
# Happy path
# ───────────────────────────────────────────────────────────────────────────


def test_pull_openai_costs_writes_records(clean_pull_state: None) -> None:
    """Two cost rows (input + output line items) land as telemetry_records
    with record_type='cost', source_api='openai_admin_costs', the pinned
    external_id, no subject_user_id, and the Decimal amount preserved as
    a string in metadata."""
    from vargate_telemetry.chain import verify_telemetry_chain
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_costs import (
        SOURCE_API_OPENAI_COSTS,
        _pull_openai_costs_for_tenant,
    )

    tenant = "tnt_us_oai_costs_write"
    _provision_tenant(tenant)

    results = [
        _cost_result(
            value=0.0002225, line_item="gpt-4o-2024-08-06, input"
        ),
        _cost_result(
            value=0.0010000, line_item="gpt-4o-2024-08-06, output"
        ),
    ]
    result = _pull_openai_costs_for_tenant(
        tenant, client=_stub_client(_page_handler(results))
    )
    assert result["status"] == "ok"
    assert result["records_pulled"] == 2
    assert result["records_deduped"] == 0

    with session_scope(tenant) as s:
        rows = s.execute(
            sql_text(
                "SELECT external_id, record_type, source_api, "
                "subject_user_id, metadata::text AS meta "
                "FROM telemetry_records WHERE tenant_id = :t "
                "ORDER BY chain_seq"
            ),
            {"t": tenant},
        ).all()

    assert len(rows) == 2
    input_eid = (
        f"openai:{SOURCE_API_OPENAI_COSTS}:{_START}:{_END}:"
        f"gpt-4o-2024-08-06, input:proj_alpha"
    )
    by_eid = {r.external_id: r for r in rows}
    assert input_eid in by_eid
    row = by_eid[input_eid]
    assert row.record_type == "cost"
    assert row.source_api == SOURCE_API_OPENAI_COSTS
    # Costs have no per-user grain.
    assert row.subject_user_id is None

    meta = json.loads(row.meta)
    assert meta["line_item"] == "gpt-4o-2024-08-06, input"
    assert meta["project_id"] == "proj_alpha"
    assert meta["project_name"] == "Alpha"
    assert meta["currency"] == "usd"
    # Decimal preserved exactly as a string (no binary-float drift).
    assert Decimal(meta["amount_value"]) == Decimal("0.0002225")

    chain = verify_telemetry_chain(tenant)
    assert chain.valid is True
    assert chain.record_count == 2


def test_pull_openai_costs_handles_scientific_notation_amount(
    clean_pull_state: None,
) -> None:
    """``amount.value`` arrives in sci-notation (``1.29e-05``); it must
    round-trip to an exact Decimal in metadata, never a lossy float."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_costs import (
        _pull_openai_costs_for_tenant,
    )

    tenant = "tnt_us_oai_costs_sci"
    _provision_tenant(tenant)

    # 1.29e-05 — the recon's documented sci-notation case.
    results = [_cost_result(value=1.29e-05, line_item="o3, input")]
    _pull_openai_costs_for_tenant(
        tenant, client=_stub_client(_page_handler(results))
    )

    with session_scope(tenant) as s:
        meta = s.execute(
            sql_text(
                "SELECT metadata::text FROM telemetry_records "
                "WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).scalar_one()
    amount = Decimal(json.loads(meta)["amount_value"])
    # Decimal(str(1.29e-05)) == Decimal("1.29e-05") == 0.0000129.
    assert amount == Decimal("0.0000129")


# ───────────────────────────────────────────────────────────────────────────
# 403 soft-skip
# ───────────────────────────────────────────────────────────────────────────


def test_pull_openai_costs_skips_when_403(clean_pull_state: None) -> None:
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_costs import (
        _pull_openai_costs_for_tenant,
    )

    tenant = "tnt_us_oai_costs_403"
    _provision_tenant(tenant)

    def handler_403(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "no"}})

    result = _pull_openai_costs_for_tenant(
        tenant, client=_stub_client(handler_403)
    )
    assert result["status"] == "no_openai_costs_access"
    assert result["records_pulled"] == 0

    with session_scope(tenant) as s:
        count = s.execute(
            sql_text(
                "SELECT count(*) FROM telemetry_records WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).scalar_one()
    assert count == 0


# ───────────────────────────────────────────────────────────────────────────
# Dedup-only second run → cursor advances
# ───────────────────────────────────────────────────────────────────────────


def test_pull_openai_costs_dedup_only_advances_cursor(
    clean_pull_state: None,
) -> None:
    from vargate_telemetry.db import engine
    from vargate_telemetry.tasks.pull_openai_costs import (
        SOURCE_API_OPENAI_COSTS,
        _pull_openai_costs_for_tenant,
    )

    tenant = "tnt_us_oai_costs_dedup"
    _provision_tenant(tenant)
    results = [_cost_result()]

    first = _pull_openai_costs_for_tenant(
        tenant, client=_stub_client(_page_handler(results))
    )
    assert first["records_pulled"] == 1
    assert first["records_deduped"] == 0

    second = _pull_openai_costs_for_tenant(
        tenant, client=_stub_client(_page_handler(results))
    )
    assert second["status"] == "ok"
    assert second["records_pulled"] == 0
    assert second["records_deduped"] == 1

    with engine.begin() as conn:
        status = conn.execute(
            sql_text(
                "SELECT last_status FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :s"
            ),
            {"t": tenant, "s": SOURCE_API_OPENAI_COSTS},
        ).scalar_one()
    assert status == "ok"


# ───────────────────────────────────────────────────────────────────────────
# Empty bucket → sentinel
# ───────────────────────────────────────────────────────────────────────────


def test_pull_openai_costs_empty_bucket_emits_sentinel(
    clean_pull_state: None,
) -> None:
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_costs import (
        SOURCE_API_OPENAI_COSTS,
        _pull_openai_costs_for_tenant,
    )

    tenant = "tnt_us_oai_costs_empty"
    _provision_tenant(tenant)

    result = _pull_openai_costs_for_tenant(
        tenant, client=_stub_client(_page_handler([]))
    )
    assert result["status"] == "ok"
    assert result["records_pulled"] == 1

    with session_scope(tenant) as s:
        row = s.execute(
            sql_text(
                "SELECT external_id, metadata::text AS meta "
                "FROM telemetry_records WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).one()
    assert row.external_id == (
        f"openai:{SOURCE_API_OPENAI_COSTS}:{_START}:{_END}:-:-"
    )
    assert json.loads(row.meta)["result"] is None


# ───────────────────────────────────────────────────────────────────────────
# Dispatcher
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def dispatch_tenants() -> Iterator[dict]:
    import uuid as _uuid

    from vargate_telemetry.db import engine

    sfx = _uuid.uuid4().hex[:8]
    ids = {
        "us_active": f"t-oaic-us-{sfx}",
        "eu_active": f"t-oaic-eu-{sfx}",
        "us_inactive": f"t-oaic-ui-{sfx}",
    }
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "billing_status) VALUES "
                "(:ua, 'us', true, 'paying'), "
                "(:ea, 'eu', true, 'paying'), "
                "(:ui, 'us', false, 'cancelled')"
            ),
            {
                "ua": ids["us_active"],
                "ea": ids["eu_active"],
                "ui": ids["us_inactive"],
            },
        )
    yield ids
    with engine.begin() as conn:
        conn.execute(
            sql_text("DELETE FROM tenants WHERE tenant_id = ANY(:ids)"),
            {"ids": list(ids.values())},
        )


def test_dispatch_openai_costs_default_all_regions(
    dispatch_tenants: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vargate_telemetry.tasks import pull_openai_costs

    dispatched: list[str] = []
    monkeypatch.setattr(
        pull_openai_costs.pull_openai_costs_for_tenant,
        "delay",
        lambda tenant_id: dispatched.append(tenant_id),
    )
    pull_openai_costs.dispatch_openai_costs_pulls()
    ds = set(dispatched)
    assert {
        dispatch_tenants["us_active"],
        dispatch_tenants["eu_active"],
    } <= ds
    assert dispatch_tenants["us_inactive"] not in ds


def test_dispatch_openai_costs_explicit_region_filters(
    dispatch_tenants: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vargate_telemetry.tasks import pull_openai_costs

    dispatched: list[str] = []
    monkeypatch.setattr(
        pull_openai_costs.pull_openai_costs_for_tenant,
        "delay",
        lambda tenant_id: dispatched.append(tenant_id),
    )
    pull_openai_costs.dispatch_openai_costs_pulls(region="eu")
    ds = set(dispatched)
    assert dispatch_tenants["eu_active"] in ds
    assert dispatch_tenants["us_active"] not in ds
