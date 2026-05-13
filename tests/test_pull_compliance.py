# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the T5.3 Compliance API ingestion pipeline.

Activity Feed ingestion (the shipping piece of T5.3):

  - ``test_pull_activities_writes_metadata_to_postgres`` — happy path,
    two activities, chain integrity holds.
  - ``test_pull_activities_is_idempotent_via_external_id`` — re-run
    dedups on the (tenant_id, source_api, external_id) UNIQUE.
  - ``test_pull_activities_increments_metering`` — Redis counter
    advances by activity count.
  - ``test_pull_activities_advances_cursor`` — pull_state cursor
    moves to max(activity.created_at) on success.
  - ``test_pull_activities_skips_when_no_activity_feed_access`` —
    InsufficientScope from the client surfaces as a soft skip
    (status="no_activity_feed_access"), not a Celery retry.

Dispatcher (Content stream stub):

  - ``test_dispatch_compliance_content_skips_when_not_configured`` —
    iterating active tenants raises NotConfigured per-tenant; the
    dispatcher counts skips, logs each, and returns cleanly.

Capability detection (T4.4 → T5.3 contract update):

  - ``test_validate_key_detects_no_activity_feed_when_403`` — list_activities
    raises InsufficientScope; validate_key returns activity_feed=False.
  - ``test_validate_key_returns_four_bool_capability_shape`` — the
    new ``KeyCapabilities`` carries ``admin_api``, ``activity_feed``,
    ``content_capture``, ``code_analytics``. ``content_capture`` is
    always False in T5.3.

All tests use ``httpx.MockTransport`` for deterministic response
sequences — same pattern as ``test_anthropic_compliance.py`` and
``test_pull_admin.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

import httpx
import pytest
from sqlalchemy import text as sql_text

from vargate_telemetry.anthropic import AnthropicAdminClient


# ───────────────────────────────────────────────────────────────────────────
# Sample activity payloads
# ───────────────────────────────────────────────────────────────────────────


_ACTIVITY_T1 = "2026-05-09T08:09:10Z"
_ACTIVITY_T2 = "2026-05-09T08:09:11Z"


def _activity_dict(
    id: str, created_at: str, type_: str = "claude_chat_created"
) -> dict:
    return {
        "id": id,
        "created_at": created_at,
        "organization_id": "org_01TEST",
        "organization_uuid": "abcdef01-2345-6789-abcd-ef0123456789",
        "actor": {
            "type": "user_actor",
            "email_address": "user@example.com",
            "user_id": "user_01TEST",
            "ip_address": "192.0.2.10",
            "user_agent": "Mozilla/5.0",
        },
        "type": type_,
        # Type-specific extra fields ride along via extra="allow" on
        # the Activity model. They land in record_metadata when we
        # store the activity.
        "claude_chat_id": f"claude_chat_{id}",
        "claude_project_id": "claude_proj_01TEST",
    }


def _two_activity_handler(request: httpx.Request) -> httpx.Response:
    """Returns two activities (newest-first per the API's sort order)."""
    return httpx.Response(
        200,
        json={
            "data": [
                _activity_dict("activity_01TEST_B", _ACTIVITY_T2),
                _activity_dict("activity_01TEST_A", _ACTIVITY_T1),
            ],
            "has_more": False,
            "first_id": "activity_01TEST_B",
            "last_id": "activity_01TEST_A",
        },
    )


def _stub_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AnthropicAdminClient:
    """Client wired with MockTransport + zero retry-wait."""
    return AnthropicAdminClient(
        api_key="test-key",
        base_url="https://api.test",
        min_wait=0.0,
        max_wait=0.0,
        wait_multiplier=0.0,
        transport=httpx.MockTransport(handler),
    )


# ───────────────────────────────────────────────────────────────────────────
# Fixture: clean state for ingestion tests
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_pull_state() -> Iterator[None]:
    """Empty every ingest-touched table + Redis meter keys.

    Mirrors the test_pull_admin fixture — same scope (telemetry_records,
    pull_state, tenants, billing) so the Activity Feed tests don't trip
    on residue from an Admin pull test or vice versa.
    """
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


# ───────────────────────────────────────────────────────────────────────────
# Activity Feed ingestion tests
# ───────────────────────────────────────────────────────────────────────────


def test_pull_activities_writes_metadata_to_postgres(
    clean_pull_state: None,
) -> None:
    """Two activities → two telemetry_records rows with type='activity',
    source_api='compliance_activities', and the full activity dict in
    record_metadata (including the type-specific extra fields)."""
    from vargate_telemetry.chain import verify_telemetry_chain
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_compliance import (
        SOURCE_API_ACTIVITIES,
        _pull_activities_for_tenant,
    )

    tenant = "test-activity-write"
    since = datetime(2026, 5, 9, tzinfo=timezone.utc)

    result = _pull_activities_for_tenant(
        tenant,
        since=since,
        client=_stub_client(_two_activity_handler),
    )

    assert result["status"] == "ok"
    assert result["activities_pulled"] == 2
    assert result["activities_deduped"] == 0

    # Read back the rows. SQL column name is `metadata` (the Python
    # attribute is `record_metadata`, mapped to the SQL `metadata`
    # column to avoid SQLAlchemy's reserved-name collision).
    with session_scope(tenant) as s:
        rows = s.execute(
            sql_text(
                "SELECT external_id, record_type, source_api, "
                "       metadata::text AS meta_json "
                "FROM telemetry_records "
                "WHERE tenant_id = :t "
                "ORDER BY chain_seq"
            ),
            {"t": tenant},
        ).all()

    assert len(rows) == 2
    assert {r.external_id for r in rows} == {
        "activity_01TEST_A",
        "activity_01TEST_B",
    }
    for r in rows:
        assert r.record_type == "activity"
        assert r.source_api == SOURCE_API_ACTIVITIES
        meta = json.loads(r.meta_json)
        # Type-specific extras survived via extra="allow"
        assert meta["claude_chat_id"].startswith("claude_chat_activity_01TEST")
        assert meta["actor"]["type"] == "user_actor"

    # Chain holds — content_hash bound into canonical bytes, both
    # records linked into the per-tenant chain.
    chain_result = verify_telemetry_chain(tenant)
    assert chain_result.valid is True
    assert chain_result.record_count == 2


def test_pull_activities_is_idempotent_via_external_id(
    clean_pull_state: None,
) -> None:
    """Calling twice with the same response yields the same two rows
    on the first call and zero new rows + two dedups on the second.

    The UNIQUE (tenant_id, source_api, external_id) constraint is the
    enforcement; `_pull_activities_for_tenant` catches IntegrityError
    and counts the dedup.
    """
    from vargate_telemetry.tasks.pull_compliance import (
        _pull_activities_for_tenant,
    )

    tenant = "test-activity-idempotent"
    since = datetime(2026, 5, 9, tzinfo=timezone.utc)

    first = _pull_activities_for_tenant(
        tenant,
        since=since,
        client=_stub_client(_two_activity_handler),
    )
    assert first["activities_pulled"] == 2
    assert first["activities_deduped"] == 0

    second = _pull_activities_for_tenant(
        tenant,
        since=since,
        client=_stub_client(_two_activity_handler),
    )
    assert second["activities_pulled"] == 0
    assert second["activities_deduped"] == 2


def test_pull_activities_increments_metering(
    clean_pull_state: None,
) -> None:
    """The Redis 'activity' counter advances by the number of inserted
    rows. Dedup hits do NOT increment (matches the pull_admin
    convention; metering counts NEW work, not iteration count)."""
    from vargate_telemetry.metering import _redis
    from vargate_telemetry.tasks.pull_compliance import (
        _pull_activities_for_tenant,
    )

    tenant = "test-activity-meter"
    since = datetime(2026, 5, 9, tzinfo=timezone.utc)

    _pull_activities_for_tenant(
        tenant,
        since=since,
        client=_stub_client(_two_activity_handler),
    )

    # Meter shape per `vargate_telemetry.metering`: one Redis hash at
    # `vargate:meter:active`, fields keyed by
    # `{tenant_id}\x1f{record_type}\x1f{bucket_iso}`. We sum every
    # field whose tenant+kind matches "{tenant}:activity" — the
    # bucket_iso varies if the flush task ran mid-test, so we don't
    # pin the bucket.
    r = _redis()
    h = r.hgetall("vargate:meter:active")
    total = 0
    for field, value in h.items():
        f = field if isinstance(field, str) else field.decode()
        v = value if isinstance(value, str) else value.decode()
        parts = f.split("\x1f")
        if len(parts) == 3 and parts[0] == tenant and parts[1] == "activity":
            total += int(v)
    assert total == 2, (
        f"expected 2 activity-meter increments for {tenant}, got {total}; "
        f"hash had fields {[k if isinstance(k, str) else k.decode() for k in h.keys()][:10]}"
    )


def test_pull_activities_advances_cursor(
    clean_pull_state: None,
) -> None:
    """After a successful pull, the (tenant, 'compliance_activities')
    cursor in pull_state equals max(activity.created_at). Subsequent
    pulls start from that cursor — pinned indirectly by the
    idempotency test, but also asserted explicitly here on the row."""
    from vargate_telemetry.db import engine
    from vargate_telemetry.tasks.pull_compliance import (
        SOURCE_API_ACTIVITIES,
        _pull_activities_for_tenant,
    )

    tenant = "test-activity-cursor"
    since = datetime(2026, 5, 9, 0, 0, tzinfo=timezone.utc)

    _pull_activities_for_tenant(
        tenant,
        since=since,
        client=_stub_client(_two_activity_handler),
    )

    with engine.begin() as conn:
        row = conn.execute(
            sql_text(
                "SELECT cursor FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :s"
            ),
            {"t": tenant, "s": SOURCE_API_ACTIVITIES},
        ).first()

    assert row is not None
    saved_cursor = datetime.fromisoformat(row.cursor)
    # The cursor is the max created_at across both activities — _T2
    # (2026-05-09T08:09:11Z) per `_two_activity_handler`.
    expected = datetime(2026, 5, 9, 8, 9, 11, tzinfo=timezone.utc)
    assert saved_cursor == expected


def test_pull_activities_skips_when_no_activity_feed_access(
    clean_pull_state: None,
) -> None:
    """Stub a 403 from the Compliance API. The pure-Python implementation
    catches InsufficientScope and returns a soft-skip dict with
    `status='no_activity_feed_access'` rather than propagating the
    exception. The Celery wrapper would NOT retry on this — the
    dispatcher sees a successful return and the tenant gets skipped
    this tick.
    """
    from vargate_telemetry.tasks.pull_compliance import (
        _pull_activities_for_tenant,
    )

    def handler_403(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "type": "permission_error",
                    "message": "scope read:compliance_activities required",
                }
            },
        )

    tenant = "test-activity-no-access"
    result = _pull_activities_for_tenant(
        tenant,
        since=datetime(2026, 5, 9, tzinfo=timezone.utc),
        client=_stub_client(handler_403),
    )

    assert result["status"] == "no_activity_feed_access"
    assert result["activities_pulled"] == 0
    assert result["activities_deduped"] == 0


# ───────────────────────────────────────────────────────────────────────────
# Dispatcher (Content stream stub)
# ───────────────────────────────────────────────────────────────────────────


def test_dispatch_compliance_content_skips_when_not_configured(
    clean_pull_state: None,
) -> None:
    """The content dispatcher iterates active tenants and calls
    `_pull_content_for_tenant` per row. Every call raises
    NotConfigured today (no tenant has a sealed Compliance Access
    Key), so the dispatcher counts the skips and returns the tenant
    count. The return value is the tenant count, not the skip count
    — the dispatcher's own metric is "how many we ATTEMPTED."
    """
    from vargate_telemetry.db import engine
    from vargate_telemetry.tasks.pull_compliance import (
        dispatch_compliance_content_pulls,
    )

    # Insert two active tenants.
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "                     billing_status) VALUES "
                "('t-content-a', 'us', TRUE, 'trial'), "
                "('t-content-b', 'us', TRUE, 'trial')"
            )
        )

    result = dispatch_compliance_content_pulls(region="us")

    # Two tenants attempted; the function returns the count attempted.
    # All would be skipped internally with NotConfigured (logs only).
    assert result == 2


# ───────────────────────────────────────────────────────────────────────────
# Capability-detection contract update (T4.4 → T5.3)
# ───────────────────────────────────────────────────────────────────────────


def test_validate_key_detects_no_activity_feed_when_403() -> None:
    """When `list_activities(limit=1)` raises InsufficientScope, the
    validate-key endpoint returns `activity_feed: False`.

    This is the load-bearing contract change of T5.3: previously the
    capability was set by a `list_members` probe (which works on any
    plan with admin keys), now it's set by a real probe against the
    Activity Feed (which is plan-gated to Enterprise + scope-gated).
    """
    from fastapi.testclient import TestClient

    import os

    os.environ.setdefault(
        "JWT_SIGNING_KEY",
        "test-jwt-signing-key-only-used-inside-the-test-suite-32b",
    )

    from vargate_telemetry.anthropic.exceptions import InsufficientScope
    from vargate_telemetry.api import onboarding as onboarding_routes
    from vargate_telemetry.api.app import app

    class _StubWorkspace:
        def __init__(self, name: str) -> None:
            self.name = name

    class _StubClient:
        def list_workspaces(self):
            yield _StubWorkspace(name="Test Org")

        def list_activities(self, **kwargs):
            # The 403 lives behind the iter — it should fire on the
            # first yield. `_pull_activities_for_tenant` and validate_key
            # both call `next(iter(...))`, which executes the generator
            # up to the raise.
            raise InsufficientScope(
                '{"error": {"type": "permission_error"}}',
                required_scope="read:compliance_activities",
            )

        def list_code_analytics(self, **kwargs):
            # T5.4: code_analytics is an independent capability. This
            # test focuses on the activity_feed=False path; the org's
            # Code Analytics access is gated separately. Mirror the
            # test scenario shape: this no-activity-feed org also has
            # no code_analytics access.
            raise InsufficientScope(
                '{"error": {"type": "permission_error"}}',
                required_scope="admin_api",
            )

        def list_members(self):  # for the older code path's compat
            yield from ()

        def close(self):
            pass

    onboarding_routes.set_client_factory_for_test(lambda _key: _StubClient())
    try:
        client = TestClient(app)
        # The validate-key route requires an authenticated session.
        # Mint one inline.
        from vargate_telemetry.auth.jwt import issue_session_jwt

        jwt_token = issue_session_jwt(
            user_id="00000000-0000-0000-0000-000000000001",
            email="probe@example.com",
            sso_provider="google",
            tenant_id=None,
        )
        response = client.post(
            "/onboarding/validate-key",
            json={
                "admin_key": (
                    "sk-ant-admin01-test-validate-no-activity-feed-xxx"
                )
            },
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["capabilities"] == {
            "admin_api": True,
            "activity_feed": False,
            "content_capture": False,
            "code_analytics": False,
            # TM1: no MCP rows for this test tenant → False.
            "mcp_connector": False,
        }
    finally:
        onboarding_routes.set_client_factory_for_test(None)


def test_validate_key_returns_five_bool_capability_shape() -> None:
    """Sanity: a fully-capable admin key returns activity_feed=True,
    but content_capture stays False (T5.3 always returns False for
    content_capture — it requires a Compliance Access Key the
    onboarding doesn't collect yet). TM1 added a fifth bool —
    `mcp_connector` — which is False until at least one MCP row
    arrives in `telemetry_records` (last 90 days)."""
    from fastapi.testclient import TestClient
    import os

    os.environ.setdefault(
        "JWT_SIGNING_KEY",
        "test-jwt-signing-key-only-used-inside-the-test-suite-32b",
    )

    from vargate_telemetry.api import onboarding as onboarding_routes
    from vargate_telemetry.api.app import app

    class _StubWorkspace:
        def __init__(self, name: str) -> None:
            self.name = name

    class _StubActivity:
        id = "activity_01_probe"
        type = "claude_chat_created"

    class _StubClient:
        def list_workspaces(self):
            yield _StubWorkspace(name="Big Enterprise Co")

        def list_activities(self, **kwargs):
            yield _StubActivity()

        def list_code_analytics(self, **kwargs):
            # T5.4: fully-capable admin key gets code_analytics=True
            # too. Empty iterator = endpoint reachable.
            yield from ()

        def list_members(self):
            yield from ()

        def close(self):
            pass

    onboarding_routes.set_client_factory_for_test(lambda _key: _StubClient())
    try:
        client = TestClient(app)
        from vargate_telemetry.auth.jwt import issue_session_jwt

        jwt_token = issue_session_jwt(
            user_id="00000000-0000-0000-0000-000000000002",
            email="probe2@example.com",
            sso_provider="google",
            tenant_id=None,
        )
        response = client.post(
            "/onboarding/validate-key",
            json={
                "admin_key": (
                    "sk-ant-admin01-test-validate-four-bool-shape-xxxxxx"
                )
            },
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["org_name"] == "Big Enterprise Co"
        # The five-bool shape lands (TM1: was four).
        assert set(body["capabilities"].keys()) == {
            "admin_api",
            "activity_feed",
            "content_capture",
            "code_analytics",
            "mcp_connector",
        }
        assert body["capabilities"]["admin_api"] is True
        assert body["capabilities"]["activity_feed"] is True
        # T5.3 invariant: content_capture is ALWAYS False today —
        # requires a Compliance Access Key the onboarding doesn't
        # collect yet.
        assert body["capabilities"]["content_capture"] is False
        # T5.4: code_analytics is now a real live probe (was
        # hardcoded False in T5.3). The fully-capable stub returns
        # an empty iterator → endpoint reachable → True.
        assert body["capabilities"]["code_analytics"] is True
        # TM1: mcp_connector is False until a real `mcp` row
        # arrives in telemetry_records. This stub never inserts
        # one (caller has tenant_id=None anyway).
        assert body["capabilities"]["mcp_connector"] is False
    finally:
        onboarding_routes.set_client_factory_for_test(None)
