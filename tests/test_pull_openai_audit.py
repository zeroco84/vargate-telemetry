# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the TM8 OpenAI Admin audit-logs pull (``pull_openai_audit``).

Audit logs use a LIST cursor (``after=<last_id>``), and the stored
cursor is the last event id (a string) — NOT a timestamp. Scenarios:

  - happy path → events land as telemetry_records keyed by event id,
    cursor advances to the newest id;
  - EMPTY feed is NORMAL (recon §1/§8: accessible-but-unpopulated on
    PAYG) → status='no_audit_data', cursor untouched, no rows;
  - 403 → status='no_openai_audit_access' (distinct from empty);
  - dedup-only second run → cursor still advances;
  - actor identity → subject_user_id + metadata.user_email resolved from
    the (docs-modeled) nested actor shape.

Audit list envelope (recon §1): ``{object:"list", data:[entry],
first_id, last_id, has_more}``. ``effective_at`` is Unix-epoch seconds.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable, Iterator

import httpx
import pytest
from sqlalchemy import text as sql_text

from vargate_telemetry.openai import OpenAIAdminClient

_EFFECTIVE_AT = int(datetime(2026, 5, 11, 12, tzinfo=timezone.utc).timestamp())


def _audit_entry(
    *,
    event_id: str,
    event_type: str = "api_key.created",
    actor: dict | None = None,
) -> dict:
    return {
        "id": event_id,
        "type": event_type,
        "effective_at": _EFFECTIVE_AT,
        "actor": actor
        if actor is not None
        else {
            "type": "session",
            "session": {
                "user": {
                    "id": "user-alice",
                    "email": "alice@example.com",
                }
            },
        },
        "project": {"id": "proj_alpha", "name": "Alpha"},
        # event-type-specific detail rides via extra="allow".
        "api_key.created": {"id": "key_new", "data": {"scopes": ["read"]}},
    }


def _list_handler(
    entries: list[dict],
) -> Callable[[httpx.Request], httpx.Response]:
    """Single-page list response. last_id = the final entry's id (the
    cursor target); has_more=False so the paginator stops."""

    def handler(request: httpx.Request) -> httpx.Response:
        last = entries[-1]["id"] if entries else None
        first = entries[0]["id"] if entries else None
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": entries,
                "first_id": first,
                "last_id": last,
                "has_more": False,
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
# Happy path — events land, cursor advances to last id
# ───────────────────────────────────────────────────────────────────────────


def test_pull_openai_audit_writes_records(clean_pull_state: None) -> None:
    from vargate_telemetry.chain import verify_telemetry_chain
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_audit import (
        SOURCE_API_OPENAI_AUDIT,
        _pull_openai_audit_for_tenant,
    )

    tenant = "tnt_us_oai_audit_write"
    _provision_tenant(tenant)

    entries = [
        _audit_entry(event_id="evt_1", event_type="login.succeeded"),
        _audit_entry(event_id="evt_2", event_type="api_key.created"),
    ]
    result = _pull_openai_audit_for_tenant(
        tenant, client=_stub_client(_list_handler(entries))
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
    assert {r.external_id for r in rows} == {
        f"openai:{SOURCE_API_OPENAI_AUDIT}:evt_1",
        f"openai:{SOURCE_API_OPENAI_AUDIT}:evt_2",
    }
    r0 = rows[0]
    assert r0.record_type == "audit_log"
    assert r0.source_api == SOURCE_API_OPENAI_AUDIT
    # Actor resolved from the nested session.user.
    assert r0.subject_user_id == "user-alice"
    meta = json.loads(r0.meta)
    assert meta["user_email"] == "alice@example.com"
    assert meta["event_type"] == "login.succeeded"
    # Full entry stored, event-specific detail preserved via extra=allow.
    assert meta["entry"]["api_key.created"]["id"] == "key_new"

    chain = verify_telemetry_chain(tenant)
    assert chain.valid is True
    assert chain.record_count == 2

    # Cursor = last event id.
    with session_scope(tenant) as s:
        cur = s.execute(
            sql_text(
                "SELECT cursor, last_status FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :s"
            ),
            {"t": tenant, "s": SOURCE_API_OPENAI_AUDIT},
        ).one()
    assert cur.cursor == "evt_2"
    assert cur.last_status == "ok"


# ───────────────────────────────────────────────────────────────────────────
# Empty feed is NORMAL (accessible-but-unpopulated)
# ───────────────────────────────────────────────────────────────────────────


def test_pull_openai_audit_empty_feed_is_normal(
    clean_pull_state: None,
) -> None:
    """Recon §1/§8: ``/audit_logs`` returns 200 with an empty data list on
    a PAYG org. That's the steady state, NOT an error — status is
    'no_audit_data', no rows land, and the cursor is left untouched (no
    new id to advance to)."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_audit import (
        SOURCE_API_OPENAI_AUDIT,
        _pull_openai_audit_for_tenant,
    )

    tenant = "tnt_us_oai_audit_empty"
    _provision_tenant(tenant)

    result = _pull_openai_audit_for_tenant(
        tenant, client=_stub_client(_list_handler([]))
    )
    assert result["status"] == "no_audit_data"
    assert result["records_pulled"] == 0
    assert result["records_deduped"] == 0

    with session_scope(tenant) as s:
        count = s.execute(
            sql_text(
                "SELECT count(*) FROM telemetry_records WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).scalar_one()
        cur_count = s.execute(
            sql_text(
                "SELECT count(*) FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :s"
            ),
            {"t": tenant, "s": SOURCE_API_OPENAI_AUDIT},
        ).scalar_one()
    assert count == 0
    # No cursor row written — there was no id to advance to.
    assert cur_count == 0


# ───────────────────────────────────────────────────────────────────────────
# 403 soft-skip — distinct from empty
# ───────────────────────────────────────────────────────────────────────────


def test_pull_openai_audit_skips_when_403(clean_pull_state: None) -> None:
    from vargate_telemetry.tasks.pull_openai_audit import (
        _pull_openai_audit_for_tenant,
    )

    tenant = "tnt_us_oai_audit_403"
    _provision_tenant(tenant)

    def handler_403(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "no"}})

    result = _pull_openai_audit_for_tenant(
        tenant, client=_stub_client(handler_403)
    )
    # Distinct status from the empty-but-accessible path.
    assert result["status"] == "no_openai_audit_access"
    assert result["records_pulled"] == 0


# ───────────────────────────────────────────────────────────────────────────
# Dedup-only second run → cursor still advances (to the same last id)
# ───────────────────────────────────────────────────────────────────────────


def test_pull_openai_audit_dedup_only_advances_cursor(
    clean_pull_state: None,
) -> None:
    """Second pull returns the same events (MockTransport ignores
    ``after``); all dedup, but the run is still status='ok' and the
    cursor stays at the last id (we saw ids, the window's just already
    ingested)."""
    from vargate_telemetry.db import engine
    from vargate_telemetry.tasks.pull_openai_audit import (
        SOURCE_API_OPENAI_AUDIT,
        _pull_openai_audit_for_tenant,
    )

    tenant = "tnt_us_oai_audit_dedup"
    _provision_tenant(tenant)
    entries = [_audit_entry(event_id="evt_1")]

    first = _pull_openai_audit_for_tenant(
        tenant, client=_stub_client(_list_handler(entries))
    )
    assert first["records_pulled"] == 1
    assert first["records_deduped"] == 0

    second = _pull_openai_audit_for_tenant(
        tenant, client=_stub_client(_list_handler(entries))
    )
    assert second["status"] == "ok"
    assert second["records_pulled"] == 0
    assert second["records_deduped"] == 1

    with engine.begin() as conn:
        cursor = conn.execute(
            sql_text(
                "SELECT cursor FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :s"
            ),
            {"t": tenant, "s": SOURCE_API_OPENAI_AUDIT},
        ).scalar_one()
    assert cursor == "evt_1"


def test_pull_openai_audit_api_key_actor_identity(
    clean_pull_state: None,
) -> None:
    """An api_key-actor entry (no session.user) resolves
    subject_user_id from the api_key id, and carries no user_email."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_audit import (
        _pull_openai_audit_for_tenant,
    )

    tenant = "tnt_us_oai_audit_apikey"
    _provision_tenant(tenant)

    entry = _audit_entry(
        event_id="evt_api",
        event_type="project.archived",
        actor={"type": "api_key", "api_key": {"id": "key_robot"}},
    )
    _pull_openai_audit_for_tenant(
        tenant, client=_stub_client(_list_handler([entry]))
    )

    with session_scope(tenant) as s:
        row = s.execute(
            sql_text(
                "SELECT subject_user_id, metadata::text AS meta "
                "FROM telemetry_records WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).one()
    assert row.subject_user_id == "key_robot"
    assert "user_email" not in json.loads(row.meta)


# ───────────────────────────────────────────────────────────────────────────
# Dispatcher
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def dispatch_tenants() -> Iterator[dict]:
    import uuid as _uuid

    from vargate_telemetry.db import engine

    sfx = _uuid.uuid4().hex[:8]
    ids = {
        "us_active": f"t-oaia-us-{sfx}",
        "eu_active": f"t-oaia-eu-{sfx}",
        "us_inactive": f"t-oaia-ui-{sfx}",
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


def test_dispatch_openai_audit_default_all_regions(
    dispatch_tenants: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vargate_telemetry.tasks import pull_openai_audit

    dispatched: list[str] = []
    monkeypatch.setattr(
        pull_openai_audit.pull_openai_audit_for_tenant,
        "delay",
        lambda tenant_id: dispatched.append(tenant_id),
    )
    pull_openai_audit.dispatch_openai_audit_pulls()
    ds = set(dispatched)
    assert {
        dispatch_tenants["us_active"],
        dispatch_tenants["eu_active"],
    } <= ds
    assert dispatch_tenants["us_inactive"] not in ds


def test_dispatch_openai_audit_explicit_region_filters(
    dispatch_tenants: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vargate_telemetry.tasks import pull_openai_audit

    dispatched: list[str] = []
    monkeypatch.setattr(
        pull_openai_audit.pull_openai_audit_for_tenant,
        "delay",
        lambda tenant_id: dispatched.append(tenant_id),
    )
    pull_openai_audit.dispatch_openai_audit_pulls(region="eu")
    ds = set(dispatched)
    assert dispatch_tenants["eu_active"] in ds
    assert dispatch_tenants["us_active"] not in ds
