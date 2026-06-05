# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for OpenAI Admin Key onboarding (TM8 Phase C).

Two routes mirror the Anthropic validate-then-seal separation:

  - ``POST /onboarding/openai/validate-key`` — probe only, never seals;
    a rejected key returns ``valid: false`` (200) with a reason, never a
    500.
  - ``POST /onboarding/openai/submit`` — admin-gated; probes, seals the
    key under ``openai_admin_key``, enqueues the usage/costs/projects
    backfill.

Build-blind (no real ``sk-admin-`` key yet): the probe runs entirely
through a stub client (the ``set_openai_client_factory_for_test`` seam),
and the backfill dispatch through a recorder (the
``set_backfill_dispatcher_for_test`` seam). These tests ARE the
verification until the deferred live-verify (a real key submitted →
``admin`` flips in ``/me/capabilities`` → OpenAI pulls land rows).

Residue-immune pattern (per memory): every test provisions a uniquely
named tenant + user and tears down with a scoped DELETE — never a global
``TRUNCATE … CASCADE``.
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator, Optional

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text


os.environ["JWT_SIGNING_KEY"] = (
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b"
)


# ───────────────────────────────────────────────────────────────────────────
# Stub OpenAI Admin client + lightweight row stand-ins
# ───────────────────────────────────────────────────────────────────────────


class _Result:
    """Stands in for a UsageCompletionsResult — only ``user_id`` matters."""

    def __init__(self, user_id: Optional[str] = None) -> None:
        self.user_id = user_id


class _UsageBucket:
    def __init__(self, results: Optional[list[_Result]] = None) -> None:
        self.results = results or []


class _CostRow:
    def __init__(self, organization_id: Optional[str] = None) -> None:
        self.organization_id = organization_id


class _CostBucket:
    def __init__(self, results: Optional[list[_CostRow]] = None) -> None:
        self.results = results or []


class StubOpenAIClient:
    """Stands in for ``OpenAIAdminClient`` carrying a candidate Admin key.

    Each probe method either returns an iterator of stub rows or raises a
    configured exception, so every branch of ``_probe_openai_key`` is
    exercisable. Records ``calls`` + whether ``close()`` ran so tests can
    assert the probe surface and that the client is always closed.
    """

    def __init__(
        self,
        *,
        usage: Optional[list[_UsageBucket]] = None,
        costs: Optional[list[_CostBucket]] = None,
        audit: Optional[list[object]] = None,
        users: Optional[list[object]] = None,
        usage_raises: Optional[BaseException] = None,
        costs_raises: Optional[BaseException] = None,
        audit_raises: Optional[BaseException] = None,
        users_raises: Optional[BaseException] = None,
    ) -> None:
        self._usage = (
            usage
            if usage is not None
            else [_UsageBucket([_Result(user_id="user-alice")])]
        )
        self._costs = (
            costs
            if costs is not None
            else [_CostBucket([_CostRow(organization_id="org-acme")])]
        )
        self._audit = audit if audit is not None else [object()]
        self._users = users if users is not None else [object()]
        self._usage_raises = usage_raises
        self._costs_raises = costs_raises
        self._audit_raises = audit_raises
        self._users_raises = users_raises
        self.calls: list[str] = []
        self.closed = False

    def list_usage(self, **_kwargs: object) -> Iterator[_UsageBucket]:
        self.calls.append("list_usage")
        if self._usage_raises is not None:
            raise self._usage_raises
        return iter(self._usage)

    def list_costs(self, **_kwargs: object) -> Iterator[_CostBucket]:
        self.calls.append("list_costs")
        if self._costs_raises is not None:
            raise self._costs_raises
        return iter(self._costs)

    def list_audit_logs(self, **_kwargs: object) -> Iterator[object]:
        self.calls.append("list_audit_logs")
        if self._audit_raises is not None:
            raise self._audit_raises
        return iter(self._audit)

    def list_users(self, **_kwargs: object) -> Iterator[object]:
        self.calls.append("list_users")
        if self._users_raises is not None:
            raise self._users_raises
        return iter(self._users)

    def close(self) -> None:
        self.closed = True


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.openai.com/test")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=request, response=response
    )


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_seams() -> Iterator[None]:
    """Reset the injectable probe-client factory + backfill dispatcher
    before AND after every test."""
    from vargate_telemetry.api.openai_onboarding import (
        set_backfill_dispatcher_for_test,
        set_openai_client_factory_for_test,
    )

    set_openai_client_factory_for_test(None)
    set_backfill_dispatcher_for_test(None)
    yield
    set_openai_client_factory_for_test(None)
    set_backfill_dispatcher_for_test(None)


@pytest.fixture
def make_tenant_user():
    """Factory: provision a uniquely-named tenant + DEK + user (admin by
    default). Scoped DELETE teardown — never a global truncate."""
    from vargate_telemetry.crypto.seal import provision_tenant_dek
    from vargate_telemetry.db import engine

    created: list[str] = []

    def _make(role: str = "admin") -> tuple[str, uuid.UUID]:
        sfx = uuid.uuid4().hex[:12]
        tenant_id = f"tnt_us_oai_onb_{sfx}"
        user_uuid = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                sql_text(
                    "INSERT INTO tenants "
                    "(tenant_id, region, active, billing_status) "
                    "VALUES (:t, 'us', true, 'paying')"
                ),
                {"t": tenant_id},
            )
            conn.execute(
                sql_text(
                    """
                    INSERT INTO users
                        (id, email, sso_provider, sso_subject_id, name,
                         tenant_id, role)
                    VALUES
                        (:id, :email, 'google', :sub, 'Tester', :t, :role)
                    """
                ),
                {
                    "id": str(user_uuid),
                    "email": f"oai-{sfx}@example.com",
                    "sub": f"google-sub-{sfx}",
                    "t": tenant_id,
                    "role": role,
                },
            )
        # DEK must exist before seal_secret can run (the Anthropic
        # select-region step provisions it in production).
        provision_tenant_dek(tenant_id)
        created.append(tenant_id)
        return tenant_id, user_uuid

    yield _make

    with engine.begin() as conn:
        for table in (
            "encrypted_secrets",
            "tenant_deks",
            "users",
            "tenants",
        ):
            conn.execute(
                sql_text(
                    f"DELETE FROM {table} WHERE tenant_id = ANY(:ids)"
                ),
                {"ids": created},
            )


def _bearer(user_uuid: uuid.UUID, tenant_id: Optional[str]) -> str:
    from vargate_telemetry.auth.jwt import issue_session_jwt

    return issue_session_jwt(
        user_id=str(user_uuid),
        email="tester@example.com",
        sso_provider="google",
        tenant_id=tenant_id,
    )


def _install_stub(stub: StubOpenAIClient) -> None:
    from vargate_telemetry.api.openai_onboarding import (
        set_openai_client_factory_for_test,
    )

    set_openai_client_factory_for_test(lambda _key: stub)


def _record_backfill() -> list[str]:
    """Install a recorder for the backfill dispatcher; returns the list it
    appends the tenant_id to so tests can assert it fired (and once)."""
    from vargate_telemetry.api.openai_onboarding import (
        BACKFILL_STREAMS,
        set_backfill_dispatcher_for_test,
    )

    fired: list[str] = []

    def _dispatch(tenant_id: str) -> list[str]:
        fired.append(tenant_id)
        return list(BACKFILL_STREAMS)

    set_backfill_dispatcher_for_test(_dispatch)
    return fired


def _is_sealed(tenant_id: str) -> bool:
    from vargate_telemetry.crypto.seal import unseal_secret
    from vargate_telemetry.openai.factory import OPENAI_ADMIN_KEY_SECRET

    try:
        unseal_secret(tenant_id, OPENAI_ADMIN_KEY_SECRET)
        return True
    except LookupError:
        return False


_VALID_KEY = "sk-admin-" + "v" * 40


def _post_validate(
    client: TestClient,
    user_uuid: uuid.UUID,
    tenant_id: Optional[str],
    key: str,
) -> httpx.Response:
    return client.post(
        "/onboarding/openai/validate-key",
        json={"admin_key": key},
        headers={"Authorization": f"Bearer {_bearer(user_uuid, tenant_id)}"},
    )


def _post_submit(
    client: TestClient,
    user_uuid: uuid.UUID,
    tenant_id: Optional[str],
    key: str,
) -> httpx.Response:
    return client.post(
        "/onboarding/openai/submit",
        json={"admin_key": key},
        headers={"Authorization": f"Bearer {_bearer(user_uuid, tenant_id)}"},
    )


# ═══════════════════════════════════════════════════════════════════════════
# validate-key — probe only, never seals
# ═══════════════════════════════════════════════════════════════════════════


def test_validate_valid_key_returns_full_checklist(
    client: TestClient, make_tenant_user
) -> None:
    tenant_id, user_uuid = make_tenant_user(role="admin")
    stub = StubOpenAIClient()  # all endpoints populated, user_id present
    _install_stub(stub)

    resp = _post_validate(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    assert body["reason"] is None
    assert body["org_id"] == "org-acme"
    caps = body["capabilities"]
    assert caps == {
        "admin": True,
        "costs": True,
        "audit_logs": True,
        "project_users": True,
        "per_user_breakdown": True,
    }
    # Probed all four endpoint families, then closed the client.
    assert stub.calls == [
        "list_usage",
        "list_costs",
        "list_audit_logs",
        "list_users",
    ]
    assert stub.closed is True
    # Crucially: validate-key NEVER seals.
    assert not _is_sealed(tenant_id)


def test_validate_member_can_probe_without_admin(
    client: TestClient, make_tenant_user
) -> None:
    """validate-key only requires a signed-in user (read-only probe, no
    tenant side effects) — a member can run it; the admin gate is on
    submit."""
    tenant_id, user_uuid = make_tenant_user(role="member")
    _install_stub(StubOpenAIClient())

    resp = _post_validate(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 200, resp.text
    assert resp.json()["valid"] is True


def test_validate_per_user_breakdown_false_when_user_id_null(
    client: TestClient, make_tenant_user
) -> None:
    """A usage row with no ``user_id`` (group_by=user_id didn't populate on
    a coarser tier) → per_user_breakdown False, but the key is still
    valid."""
    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(
        StubOpenAIClient(usage=[_UsageBucket([_Result(user_id=None)])])
    )

    resp = _post_validate(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    assert body["capabilities"]["admin"] is True
    assert body["capabilities"]["per_user_breakdown"] is False


def test_validate_403_on_admin_probe_is_valid_false_not_500(
    client: TestClient, make_tenant_user
) -> None:
    """A key that 403s the usage (admin) probe is unusable → valid:false
    with a reason, NEVER a 500."""
    from vargate_telemetry.openai import InsufficientScope

    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(
        StubOpenAIClient(usage_raises=InsufficientScope("forbidden"))
    )

    resp = _post_validate(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    assert body["reason"]
    assert body["capabilities"]["admin"] is False
    assert not _is_sealed(tenant_id)


def test_validate_401_on_admin_probe_is_valid_false(
    client: TestClient, make_tenant_user
) -> None:
    """A bare 401 on the admin probe → valid:false, NOT a 500.

    The real client raises ``InsufficientScope`` only on 403; a 401
    surfaces as ``httpx.HTTPStatusError`` from ``raise_for_status()``.
    ``_is_auth_failure`` recognizes both 401 and 403, so the route maps a
    401 to the same valid:false path — pinning the spec contract that a
    bad key never 500s."""
    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(
        StubOpenAIClient(usage_raises=_make_http_status_error(401))
    )

    resp = _post_validate(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 200, resp.text
    assert resp.json()["valid"] is False
    assert not _is_sealed(tenant_id)


def test_validate_partial_scope_some_caps_false_but_valid(
    client: TestClient, make_tenant_user
) -> None:
    """admin/usage works but costs + audit 403 (scope-limited key) → the
    key is valid, those caps are False, org_id is null (costs is where
    org_id comes from)."""
    from vargate_telemetry.openai import InsufficientScope

    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(
        StubOpenAIClient(
            costs_raises=InsufficientScope("forbidden"),
            audit_raises=InsufficientScope("forbidden"),
        )
    )

    resp = _post_validate(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    caps = body["capabilities"]
    assert caps["admin"] is True
    assert caps["costs"] is False
    assert caps["audit_logs"] is False
    assert caps["project_users"] is True
    assert body["org_id"] is None


def test_validate_wrong_key_type_no_network(
    client: TestClient, make_tenant_user
) -> None:
    """A standard project key (`sk-…`, not `sk-admin-`) → valid:false with
    the wrong-key-type reason and NO probe call (format-guarded)."""
    tenant_id, user_uuid = make_tenant_user(role="admin")
    stub = StubOpenAIClient()
    _install_stub(stub)

    resp = _post_validate(
        client, user_uuid, tenant_id, "sk-proj-" + "x" * 40
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    assert "Admin key" in body["reason"]
    assert stub.calls == []  # never probed
    assert stub.closed is False


def test_validate_rate_limited_returns_503(
    client: TestClient, make_tenant_user
) -> None:
    from vargate_telemetry.openai import RateLimited

    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(
        StubOpenAIClient(usage_raises=RateLimited(retry_after=7))
    )

    resp = _post_validate(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "openai_rate_limited"


def test_validate_unauthenticated_is_401(client: TestClient) -> None:
    resp = client.post(
        "/onboarding/openai/validate-key",
        json={"admin_key": _VALID_KEY},
    )
    assert resp.status_code == 401, resp.text


# ═══════════════════════════════════════════════════════════════════════════
# submit — admin-gated; probe → seal → enqueue backfill
# ═══════════════════════════════════════════════════════════════════════════


def test_submit_valid_key_seals_and_enqueues_backfill(
    client: TestClient, make_tenant_user
) -> None:
    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(StubOpenAIClient())
    fired = _record_backfill()

    resp = _post_submit(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sealed"] is True
    assert body["region"] == "us"
    assert body["org_id"] == "org-acme"
    assert body["capabilities"]["admin"] is True
    assert body["backfill_enqueued"] == [
        "openai_admin_usage",
        "openai_admin_costs",
        "openai_projects",
    ]
    # Backfill fired exactly once, for THIS tenant.
    assert fired == [tenant_id]
    # The key is sealed and round-trips back to the submitted plaintext.
    from vargate_telemetry.crypto.seal import unseal_secret
    from vargate_telemetry.openai.factory import OPENAI_ADMIN_KEY_SECRET

    assert (
        unseal_secret(tenant_id, OPENAI_ADMIN_KEY_SECRET).decode()
        == _VALID_KEY
    )


def test_submit_flips_admin_capability_in_me(
    client: TestClient, make_tenant_user
) -> None:
    """The OpenAI ``admin`` capability is False before submit, True after —
    the end-to-end signal the dashboard onboarding card reads. ``admin``
    lights on the sealed key alone (before any pull lands a row)."""
    tenant_id, user_uuid = make_tenant_user(role="admin")
    headers = {"Authorization": f"Bearer {_bearer(user_uuid, tenant_id)}"}

    before = client.get("/me/capabilities", headers=headers)
    assert before.status_code == 200, before.text
    assert before.json()["openai"]["admin"] is False

    _install_stub(StubOpenAIClient())
    _record_backfill()
    submitted = _post_submit(client, user_uuid, tenant_id, _VALID_KEY)
    assert submitted.status_code == 200, submitted.text

    after = client.get("/me/capabilities", headers=headers)
    assert after.status_code == 200, after.text
    assert after.json()["openai"]["admin"] is True


def test_submit_idempotent_reseal_rotates_key(
    client: TestClient, make_tenant_user
) -> None:
    """Re-submitting rotates the sealed key in place (UPSERT) and
    re-enqueues the backfill — both calls succeed."""
    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(StubOpenAIClient())
    fired = _record_backfill()

    first = "sk-admin-" + "a" * 40
    second = "sk-admin-" + "b" * 40
    assert (
        _post_submit(client, user_uuid, tenant_id, first).status_code == 200
    )
    assert (
        _post_submit(client, user_uuid, tenant_id, second).status_code == 200
    )

    from vargate_telemetry.crypto.seal import unseal_secret
    from vargate_telemetry.openai.factory import OPENAI_ADMIN_KEY_SECRET

    assert (
        unseal_secret(tenant_id, OPENAI_ADMIN_KEY_SECRET).decode() == second
    )
    # Backfill re-enqueued on each successful submit.
    assert fired == [tenant_id, tenant_id]


def test_submit_403_key_not_sealed_no_backfill(
    client: TestClient, make_tenant_user
) -> None:
    """A key that 403s the admin probe is rejected with a 400 and is NOT
    sealed; the backfill must not fire."""
    from vargate_telemetry.openai import InsufficientScope

    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(
        StubOpenAIClient(usage_raises=InsufficientScope("forbidden"))
    )
    fired = _record_backfill()

    resp = _post_submit(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "invalid_openai_key"
    assert not _is_sealed(tenant_id)
    assert fired == []  # backfill never dispatched


def test_submit_wrong_key_type_rejected_no_seal(
    client: TestClient, make_tenant_user
) -> None:
    tenant_id, user_uuid = make_tenant_user(role="admin")
    stub = StubOpenAIClient()
    _install_stub(stub)
    fired = _record_backfill()

    resp = _post_submit(client, user_uuid, tenant_id, "sk-" + "x" * 40)

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "wrong_key_type"
    assert stub.calls == []  # gate trips before any probe
    assert not _is_sealed(tenant_id)
    assert fired == []


def test_submit_member_is_forbidden(
    client: TestClient, make_tenant_user
) -> None:
    tenant_id, user_uuid = make_tenant_user(role="member")
    stub = StubOpenAIClient()
    _install_stub(stub)
    fired = _record_backfill()

    resp = _post_submit(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "admin_required"
    assert stub.calls == []  # gate trips before any probe
    assert not _is_sealed(tenant_id)
    assert fired == []


def test_submit_unauthenticated_is_401(client: TestClient) -> None:
    resp = client.post(
        "/onboarding/openai/submit",
        json={"admin_key": _VALID_KEY},
    )
    assert resp.status_code == 401, resp.text


def test_submit_no_tenant_bound_is_400(
    client: TestClient, make_tenant_user
) -> None:
    """A JWT with no tenant_id claim — require_admin returns
    no_tenant_bound before any probe/seal."""
    _, user_uuid = make_tenant_user(role="admin")
    stub = StubOpenAIClient()
    _install_stub(stub)

    resp = _post_submit(client, user_uuid, None, _VALID_KEY)

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "no_tenant_bound"
    assert stub.calls == []


def test_submit_rate_limited_returns_503_no_seal(
    client: TestClient, make_tenant_user
) -> None:
    from vargate_telemetry.openai import RateLimited

    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(
        StubOpenAIClient(usage_raises=RateLimited(retry_after=9))
    )
    fired = _record_backfill()

    resp = _post_submit(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "openai_rate_limited"
    assert not _is_sealed(tenant_id)
    assert fired == []


def test_submit_backfill_failure_still_seals(
    client: TestClient, make_tenant_user
) -> None:
    """The backfill dispatch is best-effort: if it raises, the key stays
    sealed and the request still succeeds (the beat will pick the streams
    up). A dispatch hiccup must not make the UI think the key didn't
    take."""
    from vargate_telemetry.api.openai_onboarding import (
        set_backfill_dispatcher_for_test,
    )

    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(StubOpenAIClient())

    def _boom(_tenant_id: str) -> list[str]:
        raise RuntimeError("broker down")

    set_backfill_dispatcher_for_test(_boom)

    resp = _post_submit(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sealed"] is True
    assert body["backfill_enqueued"] == []  # dispatch failed → empty list
    assert _is_sealed(tenant_id)  # key is sealed regardless
