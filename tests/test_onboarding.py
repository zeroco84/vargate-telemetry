# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the onboarding API (T4.4)."""

from __future__ import annotations

import os
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
    and reset the client-factory injection. Tests own provisioning.
    """
    from vargate_telemetry.api.onboarding import set_client_factory_for_test
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE sessions, users RESTART IDENTITY CASCADE")
        )
    set_client_factory_for_test(None)

    yield

    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE sessions, users RESTART IDENTITY CASCADE")
        )
    set_client_factory_for_test(None)


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


def _post_validate(
    client: TestClient, admin_key: str
) -> httpx.Response:
    return client.post(
        "/onboarding/validate-key",
        json={"admin_key": admin_key},
        headers={"Authorization": f"Bearer {_bearer_token()}"},
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
