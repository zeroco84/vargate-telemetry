# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM1 — OAuth surface tests for the MCP server.

Covers:

- ``/.well-known/oauth-authorization-server`` (RFC 8414 shape)
- ``/.well-known/oauth-protected-resource`` (RFC 9728 shape)
- ``POST /register`` (RFC 7591 DCR) — accepts claude.ai redirect_uri,
  rejects everything else.
- ``GET /authorize`` — 501 without ``MCP_SPIKE_MODE``; happy-path 302
  with code when the gate is on; rejects bad PKCE / bad
  redirect_uri / unknown client / bad response_type.
- ``POST /token`` — code exchange happy path; PKCE-verifier mismatch;
  unknown code; refresh-token rotation.

The MCP server is mounted as its own FastAPI app at
``mcp_server.main:app``. Tests against the OAuth surface don't need
the FastMCP-mounted ``/mcp`` sub-app — those are exercised in
``test_mcp_token_verifier.py`` and ``test_mcp_log_interaction.py``.

Spike-mode is toggled via the ``MCP_SPIKE_MODE`` env var. The
fixtures below set/unset it per-test so cases that need 501 and
cases that need 302 can both run in one session.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def spike_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Turn on MCP_SPIKE_MODE + populate the static test identity.

    Used by every test that needs /authorize to succeed. Tests
    that need /authorize to 501 do NOT use this fixture.
    """
    monkeypatch.setenv("MCP_SPIKE_MODE", "true")
    monkeypatch.setenv("MCP_TEST_IDENTITY_TENANT_ID", "tnt_us_test_mcp_oauth")
    monkeypatch.setenv("MCP_TEST_IDENTITY_USER_ID", "user-mcp-oauth-test")
    monkeypatch.setenv(
        "MCP_TEST_IDENTITY_USER_EMAIL", "mcp-oauth-test@example.com"
    )
    monkeypatch.setenv("MCP_SERVER_URL", "http://localhost:8002")
    yield


@pytest.fixture
def clean_mcp_oauth() -> Iterator[None]:
    """Empty mcp_oauth_clients + mcp_access_tokens before/after each test."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM mcp_access_tokens"))
        conn.execute(sql_text("DELETE FROM mcp_oauth_clients"))
    # Also clear the in-memory auth-code / refresh-token stores.
    from mcp_server.auth.oauth_routes import reset_stores_for_test

    reset_stores_for_test()
    yield
    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM mcp_access_tokens"))
        conn.execute(sql_text("DELETE FROM mcp_oauth_clients"))
    reset_stores_for_test()


@pytest.fixture
def mcp_client() -> TestClient:
    """Build a TestClient against the MCP-server FastAPI app.

    Note: this is a DIFFERENT app from ``vargate_telemetry.api.app``
    used by the onboarding tests. The MCP server runs in its own
    container (``mcp-server`` in docker-compose).
    """
    from mcp_server.main import app

    return TestClient(app)


def _pkce_pair() -> tuple[str, str]:
    """Return a (verifier, challenge) pair per RFC 7636 S256."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _register_client(
    mcp_client: TestClient,
    redirect_uri: str = "https://claude.ai/api/mcp/auth_callback",
) -> tuple[str, str]:
    """DCR a fresh client; return (client_id, client_secret)."""
    response = mcp_client.post(
        "/register",
        json={
            "client_name": "Test MCP Client",
            "redirect_uris": [redirect_uri],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["client_id"], body["client_secret"]


# ───────────────────────────────────────────────────────────────────────────
# Metadata endpoints (RFC 8414 + RFC 9728)
# ───────────────────────────────────────────────────────────────────────────


def test_authorization_server_metadata_shape(
    mcp_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RFC 8414 sanity — the metadata Claude reads to bootstrap auth."""
    monkeypatch.setenv("MCP_SERVER_URL", "http://localhost:8002")
    response = mcp_client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    body = response.json()
    assert body["issuer"] == "http://localhost:8002"
    assert body["authorization_endpoint"] == "http://localhost:8002/authorize"
    assert body["token_endpoint"] == "http://localhost:8002/token"
    assert body["registration_endpoint"] == "http://localhost:8002/register"
    assert "code" in body["response_types_supported"]
    assert "authorization_code" in body["grant_types_supported"]
    assert "refresh_token" in body["grant_types_supported"]
    assert "S256" in body["code_challenge_methods_supported"]
    assert "log_interaction" in body["scopes_supported"]


def test_protected_resource_metadata_shape(
    mcp_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RFC 9728 — Claude resolves the auth server from this doc."""
    monkeypatch.setenv("MCP_SERVER_URL", "http://localhost:8002")
    response = mcp_client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == "http://localhost:8002"
    assert body["authorization_servers"] == ["http://localhost:8002"]
    assert body["bearer_methods_supported"] == ["header"]


# ───────────────────────────────────────────────────────────────────────────
# Dynamic Client Registration (RFC 7591)
# ───────────────────────────────────────────────────────────────────────────


def test_register_accepts_claude_ai_redirect_uri(
    mcp_client: TestClient,
    clean_mcp_oauth: None,
) -> None:
    """Happy path — claude.ai callback URL is allowlisted."""
    response = mcp_client.post(
        "/register",
        json={
            "client_name": "Claude Web",
            "redirect_uris": [
                "https://claude.ai/api/mcp/auth_callback",
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["client_id"]
    assert body["client_secret"]
    assert body["client_name"] == "Claude Web"
    # The credentials should not be the same value — defence against
    # an accidental refactor that aliases client_secret to client_id.
    assert body["client_id"] != body["client_secret"]


def test_register_rejects_off_allowlist_redirect_uri(
    mcp_client: TestClient,
    clean_mcp_oauth: None,
) -> None:
    """An attacker-controlled callback URL must NOT be accepted."""
    response = mcp_client.post(
        "/register",
        json={
            "client_name": "Sketchy",
            "redirect_uris": ["https://evil.example.com/cb"],
        },
    )
    assert response.status_code == 400, response.text
    body = response.json()["detail"]
    assert body["error"] == "invalid_redirect_uri"


# ───────────────────────────────────────────────────────────────────────────
# /authorize — spike-mode gate + happy path
# ───────────────────────────────────────────────────────────────────────────


def test_authorize_without_spike_mode_redirects_to_sso_bridge(
    mcp_client: TestClient,
    clean_mcp_oauth: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TM2 contract: production /authorize redirects to Ogma SSO bridge.

    Renamed from `test_authorize_returns_501_without_spike_mode` —
    the TM1 501 behavior was the spike-only contract. TM2 wires the
    real SSO bridge, so spike-off becomes the happy production
    path. Detailed coverage of the bridge-redirect shape lives in
    test_mcp_oauth_sso_bridge.py; this test just confirms the
    contract change at the top of the OAuth surface.
    """
    monkeypatch.delenv("MCP_SPIKE_MODE", raising=False)

    client_id, _ = _register_client(mcp_client)
    verifier, challenge = _pkce_pair()

    response = mcp_client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "abc",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert "/auth/mcp-bridge" in location
    assert "state=" in location
    assert "return=" in location


def test_authorize_happy_path_emits_warning_and_redirects(
    mcp_client: TestClient,
    clean_mcp_oauth: None,
    spike_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With MCP_SPIKE_MODE on, /authorize 302s back to claude.ai with a code,
    and logs a prominent SPIKE-MODE WARNING (the contract from the founder)."""
    import logging

    client_id, _ = _register_client(mcp_client)
    verifier, challenge = _pkce_pair()
    state = "claude-state-" + secrets.token_urlsafe(8)

    with caplog.at_level(logging.WARNING, logger="mcp_server.auth.oauth_routes"):
        response = mcp_client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            },
            follow_redirects=False,
        )
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith("https://claude.ai/api/mcp/auth_callback?")
    assert f"state={state}" in location
    assert "code=" in location

    # The SPIKE MODE warning MUST appear — that's the loud-visible
    # safety rail the founder asked for ("Do not promote past TM1").
    assert any(
        "SPIKE MODE" in record.getMessage() for record in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_authorize_rejects_non_s256_pkce(
    mcp_client: TestClient,
    clean_mcp_oauth: None,
    spike_env: None,
) -> None:
    """Plain PKCE (without S256) is not accepted — security-floor."""
    client_id, _ = _register_client(mcp_client)
    response = mcp_client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "response_type": "code",
            "code_challenge": "abc",
            "code_challenge_method": "plain",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"] == "invalid_request"


def test_authorize_rejects_unknown_client(
    mcp_client: TestClient,
    clean_mcp_oauth: None,
    spike_env: None,
) -> None:
    """A bogus client_id is not minted out of thin air."""
    _, challenge = _pkce_pair()
    response = mcp_client.get(
        "/authorize",
        params={
            "client_id": "not-a-real-client-id",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"] == "invalid_client"


# ───────────────────────────────────────────────────────────────────────────
# /token — happy path + the negative cases
# ───────────────────────────────────────────────────────────────────────────


def _extract_code_from_redirect(location: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(location).query)["code"][0]


def _complete_authorize(
    mcp_client: TestClient,
) -> tuple[str, str, str]:
    """Register + authorize + return (client_id, code, verifier).

    The cases that exercise /token reuse this so they don't have
    to re-implement the dance.
    """
    client_id, _ = _register_client(mcp_client)
    verifier, challenge = _pkce_pair()
    response = mcp_client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    code = _extract_code_from_redirect(response.headers["location"])
    return client_id, code, verifier


def test_token_exchange_happy_path(
    mcp_client: TestClient,
    clean_mcp_oauth: None,
    spike_env: None,
) -> None:
    """A valid code + correct PKCE verifier → access + refresh tokens."""
    client_id, code, verifier = _complete_authorize(mcp_client)

    response = mcp_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["expires_in"] >= 60
    assert body["scope"] == "log_interaction"


def test_token_exchange_rejects_pkce_mismatch(
    mcp_client: TestClient,
    clean_mcp_oauth: None,
    spike_env: None,
) -> None:
    """A wrong verifier MUST NOT mint a token (PKCE replay defence)."""
    client_id, code, _ = _complete_authorize(mcp_client)

    response = mcp_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": "totally-wrong-verifier-value",
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "invalid_grant"


def test_token_exchange_rejects_reused_code(
    mcp_client: TestClient,
    clean_mcp_oauth: None,
    spike_env: None,
) -> None:
    """A code is one-shot — the second exchange MUST fail (replay defence).

    The first exchange consumes the code from the in-memory store;
    the second should miss and 400 with invalid_grant.
    """
    client_id, code, verifier = _complete_authorize(mcp_client)

    first = mcp_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        },
    )
    assert first.status_code == 200

    second = mcp_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        },
    )
    assert second.status_code == 400, second.text
    assert second.json()["error"] == "invalid_grant"


def test_token_refresh_returns_new_pair(
    mcp_client: TestClient,
    clean_mcp_oauth: None,
    spike_env: None,
) -> None:
    """A refresh-token exchange returns a new access + refresh pair."""
    client_id, code, verifier = _complete_authorize(mcp_client)

    initial = mcp_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        },
    ).json()
    assert "refresh_token" in initial

    refresh = mcp_client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": initial["refresh_token"],
            "client_id": client_id,
        },
    )
    assert refresh.status_code == 200, refresh.text
    body = refresh.json()
    # New tokens, not the same values.
    assert body["access_token"] != initial["access_token"]
    assert body["refresh_token"] != initial["refresh_token"]


# ───────────────────────────────────────────────────────────────────────────
# /_health probe (used by docker-compose + nginx)
# ───────────────────────────────────────────────────────────────────────────


def test_health_endpoint_reports_spike_mode_flag(
    mcp_client: TestClient,
    spike_env: None,
) -> None:
    """Ops observability — confirm spike-mode at a glance from /_health."""
    response = mcp_client.get("/_health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["spike_mode"] is True
