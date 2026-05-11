# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the onboarding API (T4.4)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text


os.environ["JWT_SIGNING_KEY"] = (
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b"
)


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_onboarding_state() -> None:
    """Clear users + sessions (the only auth-side state) before/after,
    and reset every injectable seam in the onboarding module. Tests
    own provisioning.

    `select-region` writes to additional tables (tenants, tenant_deks,
    encrypted_secrets) — those are also truncated so each test starts
    from a clean slate and the no-persist assertion in validate-key
    tests is meaningful.
    """
    from vargate_telemetry.api.onboarding import (
        set_async_result_factory_for_test,
        set_client_factory_for_test,
        set_task_dispatcher_for_test,
        set_tenant_id_generator_for_test,
    )
    from vargate_telemetry.db import engine

    truncate_sql = sql_text(
        "TRUNCATE TABLE encrypted_secrets, tenant_deks, sessions, "
        "users, tenants RESTART IDENTITY CASCADE"
    )

    with engine.begin() as conn:
        conn.execute(truncate_sql)
    set_client_factory_for_test(None)
    set_tenant_id_generator_for_test(None)
    set_task_dispatcher_for_test(None)
    set_async_result_factory_for_test(None)

    yield

    with engine.begin() as conn:
        conn.execute(truncate_sql)
    set_client_factory_for_test(None)
    set_tenant_id_generator_for_test(None)
    set_task_dispatcher_for_test(None)
    set_async_result_factory_for_test(None)


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


def _bearer_token() -> str:
    """Issue a JWT for a fake authenticated user so the protected
    endpoint accepts the request. The user doesn't need to exist in
    the DB for the middleware to validate the token — `current_user`
    only checks the JWT signature + claims."""
    from vargate_telemetry.auth.jwt import issue_session_jwt

    return issue_session_jwt(
        user_id="user-onboarding-test",
        email="tester@example.com",
        sso_provider="google",
    )


def _bearer_token_for(
    user_id: uuid.UUID,
    email: str = "tester@example.com",
    tenant_id: str | None = None,
) -> str:
    """Issue a JWT keyed to a real user UUID — needed for endpoints
    that load + update the matching `users` row (`select-region`,
    `start-backfill`, `backfill-status`).

    `email` defaults to a constant so most tests can ignore it; pass
    an explicit value when the test asserts on the reissued JWT
    claims to keep DB row + JWT in sync.

    `tenant_id` is non-null for tests that exercise endpoints which
    require the user to already be bound (T4.6's backfill pair). The
    select-region tests should pass None so the endpoint's "user is
    fresh from SSO" branch is hit.
    """
    from vargate_telemetry.auth.jwt import issue_session_jwt

    return issue_session_jwt(
        user_id=str(user_id),
        email=email,
        sso_provider="google",
        tenant_id=tenant_id,
    )


def _post_validate(
    client: TestClient, admin_key: str
) -> httpx.Response:
    return client.post(
        "/onboarding/validate-key",
        json={"admin_key": admin_key},
        headers={"Authorization": f"Bearer {_bearer_token()}"},
    )


def _create_test_user(
    email: str = "tester@example.com",
    tenant_id: str | None = None,
) -> uuid.UUID:
    """INSERT a real `users` row and return its UUID. The select-region
    endpoint loads + updates this row, so the JWT needs to point at a
    real DB identity (unlike validate-key, which only needs a valid
    signature).
    """
    from vargate_telemetry.db import engine

    user_uuid = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO users
                    (id, email, sso_provider, sso_subject_id, name,
                     tenant_id)
                VALUES
                    (:id, :email, 'google', :sub, 'Tester', :tenant_id)
                """
            ),
            {
                "id": str(user_uuid),
                "email": email,
                "sub": f"google-sub-{user_uuid.hex[:8]}",
                "tenant_id": tenant_id,
            },
        )
    return user_uuid


def _post_select_region(
    client: TestClient,
    user_uuid: uuid.UUID,
    *,
    region: str,
    admin_key: str = "sk-ant-admin01-test-key-for-onboarding-XXXXXX",
    email: str = "tester@example.com",
) -> httpx.Response:
    return client.post(
        "/onboarding/select-region",
        json={"region": region, "admin_key": admin_key},
        headers={
            "Authorization": f"Bearer {_bearer_token_for(user_uuid, email)}",
        },
    )


# ───────────────────────────────────────────────────────────────────────────
# Stub Anthropic client.
# ───────────────────────────────────────────────────────────────────────────


class _StubWorkspace:
    def __init__(self, name: str) -> None:
        self.name = name


class _StubMember:
    def __init__(self, id: str) -> None:
        self.id = id


class StubAdminClient:
    """Pretends to be `AnthropicAdminClient`. Tests configure what
    each method yields (or raises) so the endpoint's branches are
    exercised deterministically.

    Each iterable is wrapped so the endpoint's `next(iter(...), None)`
    call against the generator works the same way it would against
    the real `list_workspaces()` / `list_members()` generators.
    """

    def __init__(
        self,
        *,
        workspaces: list[_StubWorkspace] | None = None,
        members: list[_StubMember] | None = None,
        workspaces_raises: BaseException | None = None,
        members_raises: BaseException | None = None,
    ) -> None:
        self._workspaces = workspaces or []
        self._members = members or []
        self._workspaces_raises = workspaces_raises
        self._members_raises = members_raises
        self.calls: list[str] = []

    def list_workspaces(self) -> Iterator[_StubWorkspace]:
        self.calls.append("list_workspaces")
        if self._workspaces_raises is not None:
            raise self._workspaces_raises
        return iter(self._workspaces)

    def list_members(self) -> Iterator[_StubMember]:
        self.calls.append("list_members")
        if self._members_raises is not None:
            raise self._members_raises
        return iter(self._members)


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError that looks like one from a
    real `AnthropicAdminClient` call — the endpoint catches it via
    `httpx.HTTPStatusError` -> 4xx detection."""
    request = httpx.Request("GET", "https://api.anthropic.com/test")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=request, response=response
    )


# ───────────────────────────────────────────────────────────────────────────
# 1. Valid key → capability report
# ───────────────────────────────────────────────────────────────────────────


def test_validate_key_returns_capabilities_for_valid_key(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """Stubbed client returns workspaces + members → all probed
    capabilities present, org_name comes from the first workspace.
    """
    from vargate_telemetry.api.onboarding import set_client_factory_for_test

    stub = StubAdminClient(
        workspaces=[_StubWorkspace(name="Acme Corp")],
        members=[_StubMember(id="user_alice")],
    )
    set_client_factory_for_test(lambda _key: stub)

    response = _post_validate(client, "sk-ant-admin01-validvalidvalidvalidvalid")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["org_name"] == "Acme Corp"
    assert body["capabilities"] == {
        "admin_api": True,
        "compliance_api": True,
        # T4.4 hardcodes false; T5 wires the real probe.
        "code_analytics": False,
    }
    # Endpoint called both probes (matches the spec's "one page each").
    assert stub.calls == ["list_workspaces", "list_members"]


# ───────────────────────────────────────────────────────────────────────────
# 2. Invalid key → 400 with structured error code
# ───────────────────────────────────────────────────────────────────────────


def test_validate_key_returns_400_for_invalid_key(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """list_workspaces raises a 401 from Anthropic → 400 with
    `code: invalid_admin_key` per the YAML contract.
    """
    from vargate_telemetry.api.onboarding import set_client_factory_for_test

    stub = StubAdminClient(
        workspaces_raises=_make_http_status_error(401),
    )
    set_client_factory_for_test(lambda _key: stub)

    response = _post_validate(client, "sk-ant-admin01-not-a-real-key-XXXXXXXXX")
    assert response.status_code == 400, response.text

    detail = response.json()["detail"]
    assert detail["code"] == "invalid_admin_key"
    assert "Anthropic" in detail["message"]
    # We bailed out at the first probe — list_members never ran.
    assert stub.calls == ["list_workspaces"]


# ───────────────────────────────────────────────────────────────────────────
# 3. Partial capabilities — admin OK, compliance not
# ───────────────────────────────────────────────────────────────────────────


def test_validate_key_returns_partial_capabilities_when_compliance_api_unavailable(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """list_workspaces succeeds (admin scope) but list_members 403s
    (compliance scope missing) → admin_api=True, compliance_api=False.
    The endpoint still returns 200 because the key works for the
    primary admin use case.
    """
    from vargate_telemetry.api.onboarding import set_client_factory_for_test

    stub = StubAdminClient(
        workspaces=[_StubWorkspace(name="Partial Corp")],
        members_raises=_make_http_status_error(403),
    )
    set_client_factory_for_test(lambda _key: stub)

    response = _post_validate(
        client, "sk-ant-admin01-partial-key-here-XXXXXXXXXXX"
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["org_name"] == "Partial Corp"
    assert body["capabilities"] == {
        "admin_api": True,
        "compliance_api": False,
        "code_analytics": False,
    }


# ───────────────────────────────────────────────────────────────────────────
# 4. No persistence — DB state is identical before and after.
# ───────────────────────────────────────────────────────────────────────────


_TABLES_THAT_MIGHT_LEAK = [
    "users",
    "sessions",
    "tenants",
    "tenant_billing",
    "tenant_deks",
    "encrypted_secrets",
    "telemetry_records",
    "usage_records",
    "billing_retry",
    "pull_state",
]


def _row_counts() -> dict[str, int]:
    from vargate_telemetry.db import engine

    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for table in _TABLES_THAT_MIGHT_LEAK:
            counts[table] = conn.execute(
                sql_text(f"SELECT COUNT(*) FROM {table}")
            ).scalar()
    return counts


def test_validate_key_does_not_persist_anything(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """A successful validate-key probe MUST NOT write to any table.

    The key is just a transient credential at this stage; the seal
    only happens in T4.5 after region confirmation.
    """
    from vargate_telemetry.api.onboarding import set_client_factory_for_test

    stub = StubAdminClient(
        workspaces=[_StubWorkspace(name="No-Persist Co")],
        members=[_StubMember(id="user_one")],
    )
    set_client_factory_for_test(lambda _key: stub)

    before = _row_counts()

    response = _post_validate(client, "sk-ant-admin01-real-but-ephemeral-XXXX")
    assert response.status_code == 200, response.text

    after = _row_counts()
    diff = {
        table: (before[table], after[table])
        for table in _TABLES_THAT_MIGHT_LEAK
        if before[table] != after[table]
    }
    assert diff == {}, (
        f"validate-key wrote to tables it shouldn't have: {diff}"
    )


# ───────────────────────────────────────────────────────────────────────────
# select-region — T4.5
# ───────────────────────────────────────────────────────────────────────────


def _count(table: str, **where: object) -> int:
    """Count rows in `table` matching `where=value`. Returns 0 when
    no rows match."""
    from vargate_telemetry.db import engine

    if not where:
        sql = f"SELECT COUNT(*) FROM {table}"
        params: dict = {}
    else:
        clause = " AND ".join(f"{col} = :{col}" for col in where)
        sql = f"SELECT COUNT(*) FROM {table} WHERE {clause}"
        params = dict(where)
    with engine.connect() as conn:
        return conn.execute(sql_text(sql), params).scalar() or 0


def test_select_region_creates_tenant_and_seals_key(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """Happy path: a brand-new user picks `us`, the endpoint creates
    the tenant row, binds it to the user, and seals the admin key
    under the freshly provisioned DEK.
    """
    from vargate_telemetry.api.onboarding import (
        set_tenant_id_generator_for_test,
    )
    from vargate_telemetry.crypto.seal import unseal_secret

    fixed_id = "tnt_us_happy_path_0001"
    set_tenant_id_generator_for_test(lambda _r: fixed_id)
    user_uuid = _create_test_user(email="happy@example.com")

    admin_key = "sk-ant-admin01-the-real-key-XXXXXXXXXXXXXXXX"
    response = _post_select_region(
        client, user_uuid, region="us", admin_key=admin_key,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"tenant_id": fixed_id, "region": "us"}

    # DB invariants — all four writes landed.
    assert _count("tenants", tenant_id=fixed_id) == 1
    assert _count("tenant_deks", tenant_id=fixed_id) == 1
    assert _count(
        "encrypted_secrets",
        tenant_id=fixed_id,
        secret_name="anthropic_admin_key",
    ) == 1
    assert _count("users", id=str(user_uuid), tenant_id=fixed_id) == 1

    # And the sealed admin key round-trips: unseal returns the
    # original plaintext, proving DEK + AAD wiring is correct.
    plaintext = unseal_secret(fixed_id, "anthropic_admin_key")
    assert plaintext == admin_key.encode("utf-8")


def test_select_region_is_idempotent_on_retry(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """Replaying the call (same user, same region) returns 200 with
    the existing tenant — no new rows, no provisioning churn.
    """
    from vargate_telemetry.api.onboarding import (
        set_tenant_id_generator_for_test,
    )

    fixed_id = "tnt_us_idempotent_0002"
    set_tenant_id_generator_for_test(lambda _r: fixed_id)
    user_uuid = _create_test_user(email="idem@example.com")

    # First call provisions.
    first = _post_select_region(client, user_uuid, region="us")
    assert first.status_code == 200, first.text
    assert first.json()["tenant_id"] == fixed_id

    # Snapshot row counts before the replay.
    before_tenants = _count("tenants")
    before_deks = _count("tenant_deks")
    before_secrets = _count("encrypted_secrets")

    # Flip the generator to a different value so we can detect a
    # "should not have re-generated" mistake — the endpoint must
    # short-circuit on the idempotency check before consulting it.
    set_tenant_id_generator_for_test(lambda _r: "tnt_us_should_never_be_used")

    # Second call returns the same tenant. No new rows.
    second = _post_select_region(client, user_uuid, region="us")
    assert second.status_code == 200, second.text
    assert second.json() == {"tenant_id": fixed_id, "region": "us"}

    assert _count("tenants") == before_tenants
    assert _count("tenant_deks") == before_deks
    assert _count("encrypted_secrets") == before_secrets


def test_select_region_rolls_back_on_failure(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """If the tenants INSERT fails (PK conflict), the whole
    transaction rolls back — no orphan `tenant_deks` row, no
    orphan `encrypted_secrets` row, and `users.tenant_id` stays
    NULL.
    """
    from vargate_telemetry.api.onboarding import (
        set_tenant_id_generator_for_test,
    )
    from vargate_telemetry.db import engine

    collision_id = "tnt_us_collision_0003"

    # Pre-insert a tenant row with the id the generator will return.
    # The select-region INSERT will hit a PK conflict.
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO tenants (tenant_id, region, active,
                                     billing_status)
                VALUES (:t, 'us', TRUE, 'trial')
                """
            ),
            {"t": collision_id},
        )

    set_tenant_id_generator_for_test(lambda _r: collision_id)
    user_uuid = _create_test_user(email="rollback@example.com")

    response = _post_select_region(client, user_uuid, region="us")

    # The endpoint surfaces the integrity error as a 500 (default
    # FastAPI mapping for unhandled exceptions). The exact status
    # is less important than the DB invariants below.
    assert response.status_code >= 500

    # No leakage: the pre-existing tenant row is still the only one
    # for that id; no DEK row, no secret row, user.tenant_id is NULL.
    assert _count("tenants", tenant_id=collision_id) == 1
    assert _count("tenant_deks", tenant_id=collision_id) == 0
    assert _count(
        "encrypted_secrets", tenant_id=collision_id
    ) == 0

    with engine.connect() as conn:
        users_tenant = conn.execute(
            sql_text("SELECT tenant_id FROM users WHERE id = :uid"),
            {"uid": str(user_uuid)},
        ).scalar()
    assert users_tenant is None, (
        f"users.tenant_id should have been rolled back, got {users_tenant!r}"
    )


def test_select_region_rejects_invalid_region_string(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """A region outside `[us, eu]` is rejected with 422 and no DB
    writes."""
    user_uuid = _create_test_user(email="badregion@example.com")

    before_tenants = _count("tenants")
    before_users_bound = _count(
        "users", id=str(user_uuid)
    )  # the row itself stays

    response = _post_select_region(client, user_uuid, region="apac")
    assert response.status_code == 422, response.text

    body = response.json()
    detail = body["detail"]
    # Pydantic returns a list of validation issues OR our explicit
    # `{"code": ..., "message": ...}` shape from the endpoint's
    # normalized_region() guard, depending on which validator fires
    # first. Accept both — what matters is that the request was
    # rejected.
    assert detail is not None

    # No tenant rows created, user binding unchanged.
    assert _count("tenants") == before_tenants
    assert _count("users", id=str(user_uuid)) == before_users_bound
    with __import__("vargate_telemetry.db", fromlist=["engine"]).engine.connect() as conn:
        assert conn.execute(
            sql_text("SELECT tenant_id FROM users WHERE id = :uid"),
            {"uid": str(user_uuid)},
        ).scalar() is None


def test_select_region_reissues_jwt_with_tenant_id_claim(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """The endpoint sets a fresh `ogma_session` cookie carrying the
    new tenant_id claim — so the very next request the browser makes
    is already tenant-bound.
    """
    from vargate_telemetry.api.onboarding import (
        set_tenant_id_generator_for_test,
    )
    from vargate_telemetry.auth.jwt import (
        SESSION_COOKIE_NAME,
        decode_session_jwt,
    )

    fixed_id = "tnt_eu_jwt_claim_0004"
    set_tenant_id_generator_for_test(lambda _r: fixed_id)
    user_email = "jwt@example.com"
    user_uuid = _create_test_user(email=user_email)

    response = _post_select_region(
        client, user_uuid, region="eu", email=user_email
    )
    assert response.status_code == 200, response.text

    # The cookie is set on the response. `TestClient` makes the raw
    # cookie value available via `response.cookies[<name>]`.
    cookie_value = response.cookies.get(SESSION_COOKIE_NAME)
    assert cookie_value, (
        "select-region did not set the ogma_session cookie"
    )

    payload = decode_session_jwt(cookie_value)
    assert payload.tenant_id == fixed_id
    assert payload.sub == str(user_uuid)
    assert payload.email == "jwt@example.com"
    assert payload.sso == "google"


# ───────────────────────────────────────────────────────────────────────────
# start-backfill / backfill-status — T4.6
# ───────────────────────────────────────────────────────────────────────────


class _StubAsyncResult:
    """Mimics `celery.result.AsyncResult` shape we use in onboarding.py:
    `.state` and `.info`. Production reads only those two attributes."""

    def __init__(self, state: str, info: object = None) -> None:
        self.state = state
        self.info = info


class _StubDispatch:
    """Mimics what `task.delay(...)` returns — an object with `.id`.
    The test asserts on `recorded_calls` to check that the dispatcher
    was invoked with the right (tenant_id, days)."""

    def __init__(self, task_id: str) -> None:
        self.id = task_id
        self.recorded_calls: list[tuple[str, int]] = []

    def __call__(self, tenant_id: str, days: int) -> "_StubDispatch":
        self.recorded_calls.append((tenant_id, days))
        return self


def _provision_test_tenant(
    tenant_id: str,
    region: str = "us",
    initial_backfill_task_id: str | None = None,
    active: bool = True,
) -> None:
    """INSERT a tenants row directly (bypass select-region). Used by
    the T4.6 tests because they don't care about the provisioning
    transaction — only the read-side."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO tenants
                    (tenant_id, region, active, billing_status,
                     initial_backfill_task_id)
                VALUES
                    (:t, :r, :active, 'trial', :tid)
                """
            ),
            {
                "t": tenant_id,
                "r": region,
                "active": active,
                "tid": initial_backfill_task_id,
            },
        )


def _bearer_for_tenant(user_uuid: uuid.UUID, tenant_id: str) -> str:
    """Issue a session JWT for a user already bound to `tenant_id`."""
    return _bearer_token_for(user_uuid, tenant_id=tenant_id)


def test_start_backfill_enqueues_celery_task(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """The endpoint calls the dispatcher with (tenant_id, days) and
    records the returned task id on `tenants.initial_backfill_task_id`."""
    from vargate_telemetry.api.onboarding import (
        set_task_dispatcher_for_test,
    )

    tenant_id = "tnt_us_backfill_enqueue"
    _provision_test_tenant(tenant_id)
    user_uuid = _create_test_user(
        email="enqueue@example.com", tenant_id=tenant_id
    )

    dispatcher = _StubDispatch(task_id="celery-task-enqueue-001")
    set_task_dispatcher_for_test(dispatcher)

    response = client.post(
        "/onboarding/start-backfill",
        json={"tenant_id": tenant_id, "days": 90},
        headers={
            "Authorization": (
                f"Bearer {_bearer_for_tenant(user_uuid, tenant_id)}"
            ),
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"task_id": "celery-task-enqueue-001"}

    # Dispatcher invoked exactly once with the right args.
    assert dispatcher.recorded_calls == [(tenant_id, 90)]

    # Task id persisted on the tenant row so the matching status
    # endpoint can scope polling to this tenant.
    from vargate_telemetry.db import engine

    with engine.connect() as conn:
        recorded = conn.execute(
            sql_text(
                "SELECT initial_backfill_task_id FROM tenants "
                "WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        ).scalar()
    assert recorded == "celery-task-enqueue-001"


def test_start_backfill_is_idempotent_when_task_id_already_recorded(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """A second call returns the existing task id — no new dispatch."""
    from vargate_telemetry.api.onboarding import (
        set_task_dispatcher_for_test,
    )

    tenant_id = "tnt_us_idem_enqueue"
    existing_task = "celery-task-already-running"
    _provision_test_tenant(
        tenant_id, initial_backfill_task_id=existing_task
    )
    user_uuid = _create_test_user(
        email="idem-enqueue@example.com", tenant_id=tenant_id
    )

    # If the dispatcher fires we'll know — count its calls.
    dispatcher = _StubDispatch(task_id="should-not-be-used")
    set_task_dispatcher_for_test(dispatcher)

    response = client.post(
        "/onboarding/start-backfill",
        json={"tenant_id": tenant_id, "days": 90},
        headers={
            "Authorization": (
                f"Bearer {_bearer_for_tenant(user_uuid, tenant_id)}"
            ),
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"task_id": existing_task}
    assert dispatcher.recorded_calls == [], (
        "idempotent path should not re-dispatch the task"
    )


def test_start_backfill_rejects_cross_tenant_request(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """User bound to tenant X cannot kick off a backfill for tenant Y."""
    from vargate_telemetry.api.onboarding import (
        set_task_dispatcher_for_test,
    )

    my_tenant = "tnt_us_caller"
    other_tenant = "tnt_us_victim"
    _provision_test_tenant(my_tenant)
    _provision_test_tenant(other_tenant)
    user_uuid = _create_test_user(
        email="cross@example.com", tenant_id=my_tenant
    )

    # Dispatcher must NOT be invoked — gate fires before it.
    dispatcher = _StubDispatch(task_id="should-not-be-used")
    set_task_dispatcher_for_test(dispatcher)

    response = client.post(
        "/onboarding/start-backfill",
        json={"tenant_id": other_tenant, "days": 90},
        headers={
            "Authorization": (
                f"Bearer {_bearer_for_tenant(user_uuid, my_tenant)}"
            ),
        },
    )

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "tenant_mismatch"
    assert dispatcher.recorded_calls == []

    # And the foreign tenant row is untouched — no task id leaked.
    from vargate_telemetry.db import engine

    with engine.connect() as conn:
        recorded = conn.execute(
            sql_text(
                "SELECT initial_backfill_task_id FROM tenants "
                "WHERE tenant_id = :t"
            ),
            {"t": other_tenant},
        ).scalar()
    assert recorded is None


def test_backfill_status_returns_progress_during_run(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """A PROGRESS-state task surfaces chunks_processed / inserted /
    deduped from the meta dict."""
    from vargate_telemetry.api.onboarding import (
        set_async_result_factory_for_test,
    )

    tenant_id = "tnt_us_status_progress"
    task_id = "celery-task-progress-001"
    _provision_test_tenant(
        tenant_id, initial_backfill_task_id=task_id
    )
    user_uuid = _create_test_user(
        email="progress@example.com", tenant_id=tenant_id
    )

    set_async_result_factory_for_test(
        lambda tid: _StubAsyncResult(
            state="PROGRESS",
            info={"chunks_processed": 4, "inserted": 87, "deduped": 2},
        )
    )

    response = client.get(
        f"/onboarding/backfill-status/{task_id}",
        headers={
            "Authorization": (
                f"Bearer {_bearer_for_tenant(user_uuid, tenant_id)}"
            ),
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "state": "PROGRESS",
        "chunks_processed": 4,
        "inserted": 87,
        "deduped": 2,
        "error": None,
    }


def test_backfill_status_returns_success_with_final_counts(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """A SUCCESS-state task surfaces the task's return-dict counts."""
    from vargate_telemetry.api.onboarding import (
        set_async_result_factory_for_test,
    )

    tenant_id = "tnt_us_status_success"
    task_id = "celery-task-success-001"
    _provision_test_tenant(
        tenant_id, initial_backfill_task_id=task_id
    )
    user_uuid = _create_test_user(
        email="success@example.com", tenant_id=tenant_id
    )

    set_async_result_factory_for_test(
        lambda tid: _StubAsyncResult(
            state="SUCCESS",
            info={
                "chunks_processed": 13,
                "inserted": 612,
                "deduped": 8,
            },
        )
    )

    response = client.get(
        f"/onboarding/backfill-status/{task_id}",
        headers={
            "Authorization": (
                f"Bearer {_bearer_for_tenant(user_uuid, tenant_id)}"
            ),
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "SUCCESS"
    assert body["chunks_processed"] == 13
    assert body["inserted"] == 612
    assert body["deduped"] == 8
    assert body["error"] is None


def test_backfill_status_returns_failure_with_exception_summary(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """A FAILURE-state task surfaces `<class>: <message>` (no traceback)."""
    from vargate_telemetry.api.onboarding import (
        set_async_result_factory_for_test,
    )

    tenant_id = "tnt_us_status_failure"
    task_id = "celery-task-failure-001"
    _provision_test_tenant(
        tenant_id, initial_backfill_task_id=task_id
    )
    user_uuid = _create_test_user(
        email="failure@example.com", tenant_id=tenant_id
    )

    set_async_result_factory_for_test(
        lambda tid: _StubAsyncResult(
            state="FAILURE",
            info=httpx.ConnectError("anthropic unreachable"),
        )
    )

    response = client.get(
        f"/onboarding/backfill-status/{task_id}",
        headers={
            "Authorization": (
                f"Bearer {_bearer_for_tenant(user_uuid, tenant_id)}"
            ),
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "FAILURE"
    # Format: "<ExceptionClass>: <message>" — no traceback leak.
    assert body["error"] == "ConnectError: anthropic unreachable"
    # Counter fields stay None (no progress info during a failure).
    assert body["chunks_processed"] is None


def test_backfill_status_for_unknown_task_id_returns_404(
    clean_onboarding_state: None,
    client: TestClient,
) -> None:
    """Polling a task id NOT recorded against the user's tenant
    returns 404 — prevents probing for other tenants' task ids AND
    avoids Celery's PENDING-vs-unknown ambiguity."""
    from vargate_telemetry.api.onboarding import (
        set_async_result_factory_for_test,
    )

    tenant_id = "tnt_us_status_404"
    real_task = "celery-task-real-001"
    _provision_test_tenant(
        tenant_id, initial_backfill_task_id=real_task
    )
    user_uuid = _create_test_user(
        email="not-found@example.com", tenant_id=tenant_id
    )

    # Factory must NOT be called — the gate fires first.
    call_log: list[str] = []
    set_async_result_factory_for_test(
        lambda tid: call_log.append(tid) or _StubAsyncResult("PENDING")
    )

    response = client.get(
        "/onboarding/backfill-status/celery-task-unknown-XXX",
        headers={
            "Authorization": (
                f"Bearer {_bearer_for_tenant(user_uuid, tenant_id)}"
            ),
        },
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "task_not_found"
    assert call_log == [], (
        "AsyncResult should not be consulted for unrecognized task ids"
    )
