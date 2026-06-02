# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for Compliance Access Key onboarding (TM5 T5.1).

Build-blind: there's no real Compliance Access Key yet, so the live
probe is exercised entirely through a stub client (the
``set_compliance_client_factory_for_test`` seam). These tests ARE the
verification until the deferred live-verify step lands.

Residue-immune pattern (per memory): every test provisions a uniquely
named tenant + user and tears down with a scoped DELETE — never a
global ``TRUNCATE tenants CASCADE`` (that cascade-wipes other tests'
rows; it broke the t2 pipeline test once already).
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
# Stub Compliance client
# ───────────────────────────────────────────────────────────────────────────


class _StubOrg:
    def __init__(self, uuid: str, name: Optional[str]) -> None:
        self.uuid = uuid
        self.name = name


class StubComplianceClient:
    """Stands in for ``AnthropicAdminClient`` carrying a compliance key.

    Configure ``orgs`` / ``users`` to drive the happy path, or
    ``orgs_raises`` / ``users_raises`` to exercise each failure branch.
    Records ``calls`` + whether ``close()`` ran so tests can assert the
    probe order and that the client is always closed.
    """

    def __init__(
        self,
        *,
        orgs: Optional[list[_StubOrg]] = None,
        users: Optional[list[object]] = None,
        orgs_raises: Optional[BaseException] = None,
        users_raises: Optional[BaseException] = None,
    ) -> None:
        self._orgs = (
            orgs
            if orgs is not None
            else [_StubOrg("91012d09-e48b-438e-a489-1bebfd8fa6f9", "Acme Enterprise")]
        )
        self._users = users if users is not None else [object()]
        self._orgs_raises = orgs_raises
        self._users_raises = users_raises
        self.calls: list[object] = []
        self.closed = False

    def list_organizations(self) -> Iterator[_StubOrg]:
        self.calls.append("list_organizations")
        if self._orgs_raises is not None:
            raise self._orgs_raises
        return iter(self._orgs)

    def list_organization_users(
        self, org_uuid: str, *, limit: Optional[int] = None
    ) -> Iterator[object]:
        self.calls.append(("list_organization_users", org_uuid, limit))
        if self._users_raises is not None:
            raise self._users_raises
        return iter(self._users)

    def close(self) -> None:
        self.closed = True


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """An httpx.HTTPStatusError shaped like one from a real client call."""
    request = httpx.Request("GET", "https://api.anthropic.com/test")
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
def _reset_factory() -> Iterator[None]:
    """Reset the injectable client factory before + after every test."""
    from vargate_telemetry.api.compliance_key import (
        set_compliance_client_factory_for_test,
    )

    set_compliance_client_factory_for_test(None)
    yield
    set_compliance_client_factory_for_test(None)


@pytest.fixture
def make_tenant_user():
    """Factory: provision a uniquely-named tenant + DEK + user (admin by
    default). Scoped DELETE teardown for everything created — never a
    global truncate."""
    from vargate_telemetry.crypto.seal import provision_tenant_dek
    from vargate_telemetry.db import engine

    created: list[str] = []

    def _make(role: str = "admin") -> tuple[str, uuid.UUID]:
        sfx = uuid.uuid4().hex[:12]
        tenant_id = f"tnt_eu_ck_{sfx}"
        user_uuid = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                sql_text(
                    "INSERT INTO tenants "
                    "(tenant_id, region, active, billing_status) "
                    "VALUES (:t, 'eu', true, 'paying')"
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
                    "email": f"ck-{sfx}@example.com",
                    "sub": f"google-sub-{sfx}",
                    "t": tenant_id,
                    "role": role,
                },
            )
        # DEK must exist before seal_secret can run.
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


def _post_key(
    client: TestClient,
    user_uuid: uuid.UUID,
    tenant_id: Optional[str],
    key: str,
) -> httpx.Response:
    return client.post(
        "/onboarding/compliance-key",
        json={"compliance_key": key},
        headers={"Authorization": f"Bearer {_bearer(user_uuid, tenant_id)}"},
    )


def _install_stub(stub: StubComplianceClient) -> None:
    from vargate_telemetry.api.compliance_key import (
        set_compliance_client_factory_for_test,
    )

    set_compliance_client_factory_for_test(lambda _key: stub)


def _is_sealed(tenant_id: str) -> bool:
    from vargate_telemetry.anthropic import ANTHROPIC_COMPLIANCE_KEY_SECRET
    from vargate_telemetry.crypto.seal import unseal_secret

    try:
        unseal_secret(tenant_id, ANTHROPIC_COMPLIANCE_KEY_SECRET)
        return True
    except LookupError:
        return False


_VALID_KEY = "sk-ant-api01-" + "v" * 40


# ───────────────────────────────────────────────────────────────────────────
# Happy path
# ───────────────────────────────────────────────────────────────────────────


def test_valid_key_validates_seals_and_returns_org_name(
    client: TestClient, make_tenant_user
) -> None:
    tenant_id, user_uuid = make_tenant_user(role="admin")
    stub = StubComplianceClient()
    _install_stub(stub)

    resp = _post_key(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content_capture"] is True
    assert body["org_name"] == "Acme Enterprise"
    # Probe walked orgs → users (both scopes), then closed the client.
    assert stub.calls[0] == "list_organizations"
    assert stub.calls[1][0] == "list_organization_users"
    assert stub.calls[1][1] == "91012d09-e48b-438e-a489-1bebfd8fa6f9"
    assert stub.closed is True
    # The key is sealed and round-trips back to the submitted plaintext.
    from vargate_telemetry.anthropic import ANTHROPIC_COMPLIANCE_KEY_SECRET
    from vargate_telemetry.crypto.seal import unseal_secret

    assert (
        unseal_secret(tenant_id, ANTHROPIC_COMPLIANCE_KEY_SECRET).decode()
        == _VALID_KEY
    )


def test_me_capabilities_flips_content_capture_after_seal(
    client: TestClient, make_tenant_user
) -> None:
    """content_capture is False before onboarding the key, True after —
    the end-to-end capability signal the dashboard reads."""
    tenant_id, user_uuid = make_tenant_user(role="admin")
    headers = {"Authorization": f"Bearer {_bearer(user_uuid, tenant_id)}"}

    before = client.get("/me/capabilities", headers=headers)
    assert before.status_code == 200, before.text
    assert before.json()["content_capture"] is False

    _install_stub(StubComplianceClient())
    sealed = _post_key(client, user_uuid, tenant_id, _VALID_KEY)
    assert sealed.status_code == 200, sealed.text

    after = client.get("/me/capabilities", headers=headers)
    assert after.status_code == 200, after.text
    assert after.json()["content_capture"] is True


def test_resubmitting_rotates_the_sealed_key(
    client: TestClient, make_tenant_user
) -> None:
    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(StubComplianceClient())

    first = "sk-ant-api01-" + "a" * 40
    second = "sk-ant-api01-" + "b" * 40
    assert _post_key(client, user_uuid, tenant_id, first).status_code == 200
    assert _post_key(client, user_uuid, tenant_id, second).status_code == 200

    from vargate_telemetry.anthropic import ANTHROPIC_COMPLIANCE_KEY_SECRET
    from vargate_telemetry.crypto.seal import unseal_secret

    assert (
        unseal_secret(tenant_id, ANTHROPIC_COMPLIANCE_KEY_SECRET).decode()
        == second
    )


def test_empty_org_tree_still_seals_without_user_probe(
    client: TestClient, make_tenant_user
) -> None:
    """A key that authenticates but sees zero orgs still seals (it's a
    valid key); the user-scope probe is skipped + org_name is null."""
    tenant_id, user_uuid = make_tenant_user(role="admin")
    stub = StubComplianceClient(orgs=[])
    _install_stub(stub)

    resp = _post_key(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 200, resp.text
    assert resp.json()["content_capture"] is True
    assert resp.json()["org_name"] is None
    assert stub.calls == ["list_organizations"]  # no user probe
    assert _is_sealed(tenant_id)


# ───────────────────────────────────────────────────────────────────────────
# Format guards (local, no network — stub must NOT be called)
# ───────────────────────────────────────────────────────────────────────────


def test_admin_key_rejected_with_wrong_key_type(
    client: TestClient, make_tenant_user
) -> None:
    tenant_id, user_uuid = make_tenant_user(role="admin")
    stub = StubComplianceClient()
    _install_stub(stub)

    resp = _post_key(
        client, user_uuid, tenant_id, "sk-ant-admin01-" + "x" * 40
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "wrong_key_type"
    assert stub.calls == []  # never probed
    assert not _is_sealed(tenant_id)


def test_garbage_key_rejected_as_malformed(
    client: TestClient, make_tenant_user
) -> None:
    tenant_id, user_uuid = make_tenant_user(role="admin")
    stub = StubComplianceClient()
    _install_stub(stub)

    resp = _post_key(
        client, user_uuid, tenant_id, "not-a-key-at-all-but-long-enough-xx"
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "malformed_compliance_key"
    assert stub.calls == []
    assert not _is_sealed(tenant_id)


# ───────────────────────────────────────────────────────────────────────────
# Probe failures (key reaches Anthropic but can't read content)
# ───────────────────────────────────────────────────────────────────────────


def test_403_on_orgs_probe_is_insufficient_scope(
    client: TestClient, make_tenant_user
) -> None:
    from vargate_telemetry.anthropic import InsufficientScope

    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(
        StubComplianceClient(orgs_raises=InsufficientScope("forbidden"))
    )

    resp = _post_key(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "insufficient_scope"
    assert "read:compliance_org_data" in detail["message"]
    assert not _is_sealed(tenant_id)


def test_403_on_users_probe_is_insufficient_scope(
    client: TestClient, make_tenant_user
) -> None:
    """Orgs probe passes (org_data scope OK) but users probe 403s — the
    key is missing read:compliance_user_data, which content actually
    needs. Must NOT seal."""
    from vargate_telemetry.anthropic import InsufficientScope

    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(
        StubComplianceClient(users_raises=InsufficientScope("forbidden"))
    )

    resp = _post_key(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "insufficient_scope"
    assert "read:compliance_user_data" in detail["message"]
    assert not _is_sealed(tenant_id)


def test_401_on_probe_is_invalid_key(
    client: TestClient, make_tenant_user
) -> None:
    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(
        StubComplianceClient(orgs_raises=_make_http_status_error(401))
    )

    resp = _post_key(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "invalid_compliance_key"
    assert not _is_sealed(tenant_id)


def test_rate_limited_probe_returns_503(
    client: TestClient, make_tenant_user
) -> None:
    from vargate_telemetry.anthropic import RateLimited

    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(
        StubComplianceClient(orgs_raises=RateLimited(retry_after=7))
    )

    resp = _post_key(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "anthropic_rate_limited"
    assert not _is_sealed(tenant_id)


def test_5xx_on_probe_returns_502_and_does_not_seal(
    client: TestClient, make_tenant_user
) -> None:
    from vargate_telemetry.anthropic import AnthropicAPIError

    tenant_id, user_uuid = make_tenant_user(role="admin")
    _install_stub(
        StubComplianceClient(orgs_raises=AnthropicAPIError(500, "boom"))
    )

    resp = _post_key(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["code"] == "anthropic_error"
    assert not _is_sealed(tenant_id)


# ───────────────────────────────────────────────────────────────────────────
# Admin gate (TM4 require_admin)
# ───────────────────────────────────────────────────────────────────────────


def test_member_is_forbidden(
    client: TestClient, make_tenant_user
) -> None:
    tenant_id, user_uuid = make_tenant_user(role="member")
    stub = StubComplianceClient()
    _install_stub(stub)

    resp = _post_key(client, user_uuid, tenant_id, _VALID_KEY)

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "admin_required"
    assert stub.calls == []  # gate trips before any probe
    assert not _is_sealed(tenant_id)


def test_unauthenticated_is_401(client: TestClient) -> None:
    resp = client.post(
        "/onboarding/compliance-key",
        json={"compliance_key": _VALID_KEY},
    )
    assert resp.status_code == 401, resp.text


def test_no_tenant_bound_is_400(
    client: TestClient, make_tenant_user
) -> None:
    """A JWT with no tenant_id claim — require_admin returns no_tenant_bound."""
    _, user_uuid = make_tenant_user(role="admin")
    stub = StubComplianceClient()
    _install_stub(stub)

    resp = _post_key(client, user_uuid, None, _VALID_KEY)

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "no_tenant_bound"
    assert stub.calls == []
