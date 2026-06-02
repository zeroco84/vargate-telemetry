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

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator, Optional

import httpx
import pytest
from sqlalchemy import text as sql_text

from vargate_telemetry.anthropic import AnthropicAdminClient, InsufficientScope


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


@pytest.fixture
def dispatch_tenants() -> Iterator[dict]:
    """Unique-id tenants (2 us + 1 eu active, 1 eu inactive) for the
    dispatcher tests; scoped DELETE teardown. Subset/disjoint assertions
    keep this immune to other tests' tenants in the shared DB — never a
    global `TRUNCATE tenants` (that cascade-wipes other tests; it broke
    the t2 pipeline)."""
    import uuid as _uuid

    from vargate_telemetry.db import engine

    sfx = _uuid.uuid4().hex[:8]
    ids = {
        "us_1": f"t-cpdisp-us1-{sfx}",
        "us_2": f"t-cpdisp-us2-{sfx}",
        "eu_1": f"t-cpdisp-eu1-{sfx}",
        "eu_inactive": f"t-cpdisp-eui-{sfx}",
    }
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "billing_status) VALUES "
                "(:u1, 'us', true, 'trial'), (:u2, 'us', true, 'trial'), "
                "(:e1, 'eu', true, 'trial'), (:ei, 'eu', false, 'trial')"
            ),
            {
                "u1": ids["us_1"],
                "u2": ids["us_2"],
                "e1": ids["eu_1"],
                "ei": ids["eu_inactive"],
            },
        )
    yield ids
    with engine.begin() as conn:
        conn.execute(
            sql_text("DELETE FROM tenants WHERE tenant_id = ANY(:ids)"),
            {"ids": list(ids.values())},
        )


def test_dispatch_compliance_content_default_dispatches_all_regions(
    dispatch_tenants: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TM5 T5.0 + T5.2: the content dispatcher with NO region queues every
    active tenant across regions via ``pull_content_for_tenant.delay``;
    explicit region filters. (A no-key tenant soft-skips INSIDE the
    per-tenant task now — see ``test_pull_content_soft_skips_when_no_key``
    — not in the dispatcher, so the dispatcher just fans out to all.)"""
    from vargate_telemetry.tasks import pull_compliance

    dispatched: list[str] = []
    monkeypatch.setattr(
        pull_compliance.pull_content_for_tenant,
        "delay",
        lambda tenant_id: dispatched.append(tenant_id),
    )

    pull_compliance.dispatch_compliance_content_pulls()
    d = set(dispatched)
    assert {
        dispatch_tenants["us_1"],
        dispatch_tenants["us_2"],
        dispatch_tenants["eu_1"],
    } <= d
    assert dispatch_tenants["eu_inactive"] not in d

    dispatched.clear()
    pull_compliance.dispatch_compliance_content_pulls(region="eu")
    d = set(dispatched)
    assert dispatch_tenants["eu_1"] in d
    assert dispatch_tenants["us_1"] not in d


def test_dispatch_compliance_activity_default_dispatches_all_regions(
    dispatch_tenants: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TM5 T5.0 for the Activity Feed dispatcher: with NO region, every
    active tenant across regions is queued via
    `pull_activities_for_tenant.delay`; explicit region filters. Subset
    assertion — other tests' tenants may also be present."""
    from vargate_telemetry.tasks import pull_compliance

    dispatched: list[str] = []
    monkeypatch.setattr(
        pull_compliance.pull_activities_for_tenant,
        "delay",
        lambda tenant_id: dispatched.append(tenant_id),
    )

    pull_compliance.dispatch_compliance_activity_pulls()
    d = set(dispatched)
    assert {
        dispatch_tenants["us_1"],
        dispatch_tenants["us_2"],
        dispatch_tenants["eu_1"],
    } <= d
    assert dispatch_tenants["eu_inactive"] not in d

    dispatched.clear()
    pull_compliance.dispatch_compliance_activity_pulls(region="eu")
    d = set(dispatched)
    assert dispatch_tenants["eu_1"] in d
    assert dispatch_tenants["us_1"] not in d


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


# ───────────────────────────────────────────────────────────────────────────
# Content stream ingestion tests (T5.2)
#
# Build-blind: the live Compliance API is stubbed (StubContentClient) and
# MinIO storage is mocked (a fake store_fn) for the orchestration tests;
# one integration test uses the REAL store_content + retrieve_content
# round-trip (MinIO is in the test stack). Residue-immune: each test uses
# a unique tenant + scoped DELETE teardown — never a global truncate.
# ───────────────────────────────────────────────────────────────────────────

from vargate_telemetry.anthropic import (  # noqa: E402
    Chat,
    ChatWithMessages,
    Organization,
    OrgUser,
)

_ORG_UUID = "91012d09-e48b-438e-a489-1bebfd8fa6f9"


def _org_obj(uuid_: str = _ORG_UUID, name: str = "Acme Enterprise") -> Organization:
    return Organization.model_validate(
        {"uuid": uuid_, "name": name, "created_at": "2025-06-01T10:00:00Z"}
    )


def _orguser_obj(id_: str) -> OrgUser:
    return OrgUser.model_validate(
        {
            "id": id_,
            "full_name": "Test User",
            "email": "user@example.com",
            "organization_role": "user",
            "created_at": "2025-06-01T10:00:00Z",
        }
    )


def _chat_obj(
    id_: str,
    user_id: str,
    *,
    updated_at: str,
    deleted_at: Optional[str] = None,
) -> Chat:
    return Chat.model_validate(
        {
            "id": id_,
            "name": "Requirements chat",
            "created_at": "2026-05-01T00:00:00Z",
            "updated_at": updated_at,
            "deleted_at": deleted_at,
            "model": "claude-opus-4-7",
            "organization_uuid": _ORG_UUID,
            "project_id": None,
            "user": {"id": user_id, "email_address": "user@example.com"},
        }
    )


def _chat_messages_obj(
    chat_id: str, user_id: str, messages: list[dict]
) -> ChatWithMessages:
    return ChatWithMessages.model_validate(
        {
            "id": chat_id,
            "created_at": "2026-05-01T00:00:00Z",
            "user": {"id": user_id, "email_address": "user@example.com"},
            "chat_messages": messages,
        }
    )


def _msg_dict(
    id_: str,
    role: str,
    text: Optional[str],
    created_at: str = "2026-05-01T00:00:01Z",
) -> dict:
    content = [] if text is None else [{"type": "text", "text": text}]
    return {
        "id": id_,
        "role": role,
        "created_at": created_at,
        "content": content,
    }


class StubContentClient:
    """Stands in for a compliance-keyed AnthropicAdminClient — drives the
    orgs → users → chats → messages walk from in-memory fixtures."""

    def __init__(
        self,
        *,
        orgs: list[Organization],
        users_by_org: dict[str, list[OrgUser]],
        chats: list[Chat],
        messages_by_chat: dict[str, ChatWithMessages],
        orgs_raises: Optional[BaseException] = None,
    ) -> None:
        self._orgs = orgs
        self._users_by_org = users_by_org
        self._chats = chats
        self._messages_by_chat = messages_by_chat
        self._orgs_raises = orgs_raises
        self.closed = False

    def list_organizations(self) -> Iterator[Organization]:
        if self._orgs_raises is not None:
            raise self._orgs_raises
        return iter(self._orgs)

    def list_organization_users(
        self, org_uuid: str, *, limit: Optional[int] = None
    ) -> Iterator[OrgUser]:
        return iter(self._users_by_org.get(org_uuid, []))

    def list_chats(self, *, user_ids, updated_at_gte=None, **_kw):
        wanted = set(user_ids)
        return iter([c for c in self._chats if c.user.id in wanted])

    def get_chat_messages(self, chat_id: str) -> ChatWithMessages:
        return self._messages_by_chat[chat_id]

    def close(self) -> None:
        self.closed = True


def _single_chat_stub(
    *,
    messages: list[dict],
    chat_id: str = "claude_chat_T1",
    user_id: str = "user_T1",
    updated_at: str = "2026-05-20T10:00:00Z",
    deleted_at: Optional[str] = None,
) -> StubContentClient:
    chat = _chat_obj(
        chat_id, user_id, updated_at=updated_at, deleted_at=deleted_at
    )
    return StubContentClient(
        orgs=[_org_obj()],
        users_by_org={_ORG_UUID: [_orguser_obj(user_id)]},
        chats=[chat],
        messages_by_chat={
            chat_id: _chat_messages_obj(chat_id, user_id, messages)
        },
    )


def _fake_store(tenant_id: str, plaintext: bytes) -> tuple[str, bytes, int]:
    """Mock store_content: real 32-byte SHA-256 (append validates len),
    deterministic ref, no MinIO."""
    h = hashlib.sha256(plaintext).digest()
    return f"2026/05/20/{h.hex()[:24]}.enc", h, len(plaintext)


@pytest.fixture
def content_tenant() -> Iterator[str]:
    """Unique tenant + DEK for content tests; scoped DELETE teardown.
    Never a global truncate (residue-immune, per memory)."""
    from vargate_telemetry.crypto.seal import provision_tenant_dek
    from vargate_telemetry.db import engine

    tid = f"tnt_eu_cc_{uuid.uuid4().hex[:12]}"
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "billing_status) VALUES (:t, 'eu', true, 'paying')"
            ),
            {"t": tid},
        )
    provision_tenant_dek(tid)
    yield tid
    with engine.begin() as conn:
        for tbl in (
            "telemetry_records",
            "pull_state",
            "encrypted_secrets",
            "tenant_deks",
            "tenants",
        ):
            conn.execute(
                sql_text(f"DELETE FROM {tbl} WHERE tenant_id = :t"),
                {"t": tid},
            )


def _content_rows(tenant_id: str) -> list:
    from vargate_telemetry.db import session_scope

    with session_scope(tenant_id) as s:
        return s.execute(
            sql_text(
                # The DB column is `metadata` (mapped to the ORM attribute
                # `record_metadata`; `metadata` is reserved in SQLAlchemy
                # declarative). Alias it back for ergonomic row access.
                "SELECT external_id, record_type, source_api, content_ref, "
                "content_size_bytes, metadata AS record_metadata "
                "FROM telemetry_records WHERE tenant_id = :t "
                "AND source_api = 'compliance_content' ORDER BY chain_seq"
            ),
            {"t": tenant_id},
        ).all()


def test_pull_content_happy_path(content_tenant: str) -> None:
    from vargate_telemetry.tasks.pull_compliance import (
        _pull_content_for_tenant,
    )

    stub = _single_chat_stub(
        messages=[
            _msg_dict("msg_1", "user", "Help me draft requirements?"),
            _msg_dict("msg_2", "assistant", "Sure, here's a draft..."),
        ]
    )
    result = _pull_content_for_tenant(
        content_tenant, client=stub, store_fn=_fake_store
    )
    assert result == {
        "content_pulled": 2,
        "content_deduped": 0,
        "status": "ok",
    }
    assert stub.closed is False  # injected client not owned/closed by us

    rows = _content_rows(content_tenant)
    assert {r.external_id for r in rows} == {"msg_1", "msg_2"}
    assert all(r.record_type == "chat_message" for r in rows)
    assert all(
        r.content_ref and r.content_ref.endswith(".enc") for r in rows
    )
    assert all(
        r.content_size_bytes and r.content_size_bytes > 0 for r in rows
    )
    by_id = {r.external_id: r.record_metadata for r in rows}
    assert by_id["msg_1"]["chat_id"] == "claude_chat_T1"
    assert by_id["msg_1"]["role"] == "user"
    assert by_id["msg_2"]["role"] == "assistant"


def test_pull_content_dedups_on_rerun(content_tenant: str) -> None:
    from vargate_telemetry.tasks.pull_compliance import (
        _pull_content_for_tenant,
    )

    msgs = [
        _msg_dict("msg_A", "user", "first"),
        _msg_dict("msg_B", "assistant", "second"),
    ]
    first = _pull_content_for_tenant(
        content_tenant, client=_single_chat_stub(messages=msgs),
        store_fn=_fake_store,
    )
    assert first["content_pulled"] == 2
    # Same messages again: the per-message existence check dedups all.
    second = _pull_content_for_tenant(
        content_tenant, client=_single_chat_stub(messages=msgs),
        store_fn=_fake_store,
    )
    assert second == {
        "content_pulled": 0,
        "content_deduped": 2,
        "status": "ok",
    }
    assert len(_content_rows(content_tenant)) == 2


def test_pull_content_soft_skips_when_no_key(content_tenant: str) -> None:
    """No client injected + no Compliance Access Key sealed →
    compliance_client_for_tenant raises LookupError → soft skip."""
    from vargate_telemetry.tasks.pull_compliance import (
        _pull_content_for_tenant,
    )

    result = _pull_content_for_tenant(content_tenant)  # builds its own client
    assert result["status"] == "no_content_key"
    assert result["content_pulled"] == 0
    assert _content_rows(content_tenant) == []


def test_pull_content_soft_skips_on_403(content_tenant: str) -> None:
    from vargate_telemetry.tasks.pull_compliance import (
        _pull_content_for_tenant,
    )

    stub = StubContentClient(
        orgs=[],
        users_by_org={},
        chats=[],
        messages_by_chat={},
        orgs_raises=InsufficientScope("forbidden"),
    )
    result = _pull_content_for_tenant(
        content_tenant, client=stub, store_fn=_fake_store
    )
    assert result["status"] == "no_content_access"
    assert _content_rows(content_tenant) == []


def test_pull_content_advances_cursor(content_tenant: str) -> None:
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_compliance import (
        SOURCE_API_CONTENT,
        _pull_content_for_tenant,
    )

    stub = _single_chat_stub(
        messages=[_msg_dict("msg_C", "user", "hello")],
        updated_at="2026-05-25T12:00:00Z",
    )
    _pull_content_for_tenant(content_tenant, client=stub, store_fn=_fake_store)
    with session_scope(content_tenant) as s:
        row = s.execute(
            sql_text(
                "SELECT cursor, last_status FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :sa"
            ),
            {"t": content_tenant, "sa": SOURCE_API_CONTENT},
        ).first()
    assert row is not None
    assert row.last_status == "ok"
    assert datetime.fromisoformat(row.cursor) == datetime(
        2026, 5, 25, 12, 0, tzinfo=timezone.utc
    )


def test_pull_content_flags_soft_deleted_chat(content_tenant: str) -> None:
    from vargate_telemetry.tasks.pull_compliance import (
        _pull_content_for_tenant,
    )

    stub = _single_chat_stub(
        messages=[_msg_dict("msg_D", "user", "in a deleted chat")],
        deleted_at="2026-05-26T09:00:00Z",
    )
    _pull_content_for_tenant(content_tenant, client=stub, store_fn=_fake_store)
    rows = _content_rows(content_tenant)
    assert len(rows) == 1
    assert rows[0].record_metadata["chat_deleted_at"].startswith("2026-05-26")


def test_pull_content_skips_textless_message(content_tenant: str) -> None:
    from vargate_telemetry.tasks.pull_compliance import (
        _pull_content_for_tenant,
    )

    stub = _single_chat_stub(
        messages=[
            _msg_dict("msg_text", "user", "has text"),
            _msg_dict("msg_empty", "user", None),  # file-only / no text
        ]
    )
    result = _pull_content_for_tenant(
        content_tenant, client=stub, store_fn=_fake_store
    )
    assert result["content_pulled"] == 1
    assert {r.external_id for r in _content_rows(content_tenant)} == {
        "msg_text"
    }


def test_pull_content_real_storage_roundtrip(content_tenant: str) -> None:
    """Integration: real store_content (DEK + MinIO). The stored blob
    decrypts back to the message text and the record points at it. The
    Anthropic side stays stubbed (build-blind) — only local storage real."""
    from vargate_telemetry.storage.content import retrieve_content
    from vargate_telemetry.tasks.pull_compliance import (
        _pull_content_for_tenant,
    )

    text = "A real message stored end to end."
    stub = _single_chat_stub(messages=[_msg_dict("msg_real", "user", text)])
    # No store_fn => real store_content path.
    result = _pull_content_for_tenant(content_tenant, client=stub)
    assert result["content_pulled"] == 1

    rows = _content_rows(content_tenant)
    assert len(rows) == 1
    content_ref = rows[0].content_ref
    assert content_ref
    assert (
        retrieve_content(content_tenant, content_ref).decode("utf-8") == text
    )


def test_compliance_client_for_tenant_builds_from_sealed_key(
    content_tenant: str,
) -> None:
    from vargate_telemetry.anthropic import (
        ANTHROPIC_COMPLIANCE_KEY_SECRET,
        compliance_client_for_tenant,
    )
    from vargate_telemetry.crypto.seal import seal_secret

    seal_secret(
        content_tenant,
        ANTHROPIC_COMPLIANCE_KEY_SECRET,
        b"sk-ant-api01-sealedkey",
    )
    client = compliance_client_for_tenant(content_tenant, min_wait=0.0)
    try:
        assert client._client.headers["x-api-key"] == "sk-ant-api01-sealedkey"
    finally:
        client.close()


def test_compliance_client_for_tenant_lookuperror_without_key(
    content_tenant: str,
) -> None:
    from vargate_telemetry.anthropic import compliance_client_for_tenant

    with pytest.raises(LookupError):
        compliance_client_for_tenant(content_tenant)
