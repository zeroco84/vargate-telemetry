# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM2 — OAuth surface tests for the MCP server.

This file covers the parts of the OAuth surface that are NOT
already exercised by the dedicated TM2 phase-test files:

  - Metadata endpoints (RFC 8414 + RFC 9728)
  - Dynamic Client Registration (RFC 7591 POST /register)
  - Token exchange (POST /token) — happy path, PKCE-mismatch
    replay defence, code-reuse replay defence, refresh-token
    rotation. **The /token tests mint codes via the TM2 SSO-
    bridge flow** (see _complete_authorize) — no spike-mode.
  - Spike-mode WARNING regression. The spike branch is unreachable
    in production (Phase A3 startup guard refuses to boot if
    MCP_SPIKE_MODE is set without the test bypass), but the
    WARNING log line is still part of the contract for test
    fixtures that lean on the bypass.
  - /_health probe — reports the spike_mode flag for ops.

The /authorize and /authorize/callback flows have their own files:
  - test_mcp_oauth_sso_bridge.py — /authorize redirect-to-bridge
  - test_mcp_authorize_callback.py — bridge-token consumption

C5 dropped the TM1-era redundant tests (validation duplicates,
plain-spike-flow happy-paths) since the SSO files cover them
more comprehensively against the production path.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from typing import Iterator
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def spike_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Turn on MCP_SPIKE_MODE for the WARNING-regression test only.

    Production must NOT enable spike mode (the Phase A3 startup
    guard refuses to boot). Tests use this fixture in isolation
    when they specifically need to exercise the spike branch's
    behavior — currently only the WARNING-log regression.
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
def primed_verifier() -> None:
    """Seed bridge_verifier with the conftest keypair's public JWK."""
    from mcp_server.auth import bridge_verifier
    from vargate_telemetry.auth import bridge_keys

    bridge_verifier.reset_for_test()
    bridge_keys.reset_cache_for_test()
    bridge_verifier.set_jwk(bridge_keys.public_jwk())
    yield
    bridge_verifier.reset_for_test()


@pytest.fixture
def clean_mcp_oauth(primed_verifier) -> Iterator[None]:
    """Empty Redis + DB OAuth tables + in-memory stores + verifier cache."""
    from mcp_server.auth import bridge_verifier, oauth_state
    from mcp_server.auth.oauth_routes import reset_stores_for_test
    from vargate_telemetry.auth import bridge_keys
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM mcp_access_tokens"))
        conn.execute(sql_text("DELETE FROM mcp_oauth_clients"))
    reset_stores_for_test()
    oauth_state.reset_for_test()
    # reset_stores_for_test wiped the verifier; re-prime so the
    # SSO callback path works in token-exchange tests.
    bridge_verifier.set_jwk(bridge_keys.public_jwk())
    yield
    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM mcp_access_tokens"))
        conn.execute(sql_text("DELETE FROM mcp_oauth_clients"))
    reset_stores_for_test()
    oauth_state.reset_for_test()


@pytest.fixture
def mcp_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient against the MCP-server FastAPI app, spike-mode off."""
    monkeypatch.delenv("MCP_SPIKE_MODE", raising=False)
    monkeypatch.setenv("MCP_SERVER_URL", "http://localhost:8002")
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


def _complete_authorize_via_sso(
    mcp_client: TestClient,
) -> tuple[str, str, str]:
    """Run the full TM2 SSO-bridge flow to mint an auth code.

    Steps (production-equivalent):

      1. DCR registers a client.
      2. /authorize stashes OAuth state in Redis + 302s to the bridge.
        We don't actually call the bridge — we sign a JWT directly
        using the conftest keypair (the same one the verifier reads).
      3. /authorize/callback verifies the JWT, claims Redis state,
        mints + returns an auth code.

    Returns (client_id, code, verifier). Verifier is the PKCE
    secret that matches the challenge sent into /authorize, so
    /token exchange can verify cleanly.
    """
    from vargate_telemetry.auth import bridge_keys

    client_id, _ = _register_client(mcp_client)
    verifier, challenge = _pkce_pair()

    # /authorize → 302 with mcp_state in the URL.
    authorize_response = mcp_client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "claude-state-xyz",
        },
        follow_redirects=False,
    )
    assert authorize_response.status_code == 302, authorize_response.text
    mcp_state = parse_qs(
        urlparse(authorize_response.headers["location"]).query
    )["state"][0]

    # Sign a bridge JWT carrying that mcp_state + a test identity.
    bridge_token = bridge_keys.sign_bridge_token(
        tenant_id="tnt_us_token_test",
        user_id="user-token-test",
        user_email="token-test@example.com",
        mcp_state=mcp_state,
    )

    # /authorize/callback → 302 to Claude with code + state.
    callback_response = mcp_client.get(
        "/authorize/callback",
        params={"bridge_token": bridge_token},
        follow_redirects=False,
    )
    assert callback_response.status_code == 302, callback_response.text
    code = parse_qs(
        urlparse(callback_response.headers["location"]).query
    )["code"][0]
    return client_id, code, verifier


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
# /authorize — top-level contract change confirmation
# ───────────────────────────────────────────────────────────────────────────
#
# Detailed coverage of the SSO bridge redirect (state persistence,
# claude_state round-trip, env-driven URLs, validation-before-redirect)
# lives in test_mcp_oauth_sso_bridge.py. This file keeps just the
# one regression case at the top of the surface.


def test_authorize_without_spike_mode_redirects_to_sso_bridge(
    mcp_client: TestClient,
    clean_mcp_oauth: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TM2 contract: production /authorize redirects to Ogma SSO bridge."""
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


def test_spike_mode_authorize_emits_warning_log(
    mcp_client: TestClient,
    clean_mcp_oauth: None,
    spike_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The spike-mode WARNING log is still emitted when the branch fires.

    The spike branch is gated behind both MCP_SPIKE_MODE and the test
    bypass env var, so it's unreachable in production. But the
    WARNING ("SPIKE MODE: returning static test identity…") is the
    loud-visible safety rail the founder asked for in TM1 — this test
    is the regression catch if anyone ever deletes the log line.
    """
    import logging

    client_id, _ = _register_client(mcp_client)
    verifier, challenge = _pkce_pair()

    with caplog.at_level(
        logging.WARNING, logger="mcp_server.auth.oauth_routes"
    ):
        response = mcp_client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "spike-warning-state",
            },
            follow_redirects=False,
        )
    # 302 to Claude (spike branch mints code directly).
    assert response.status_code == 302
    assert "claude.ai/api/mcp/auth_callback" in response.headers["location"]
    # The WARNING log line must appear.
    assert any(
        "SPIKE MODE" in record.getMessage() for record in caplog.records
    ), [r.getMessage() for r in caplog.records]


# ───────────────────────────────────────────────────────────────────────────
# /token — exercised against the SSO-bridge minted code (not spike)
# ───────────────────────────────────────────────────────────────────────────


def test_token_exchange_happy_path(
    mcp_client: TestClient,
    clean_mcp_oauth: None,
) -> None:
    """Code + correct PKCE verifier → access + refresh tokens.

    Code is minted via the TM2 SSO bridge flow (not spike), so this
    exercises the full production code path top-to-bottom.
    """
    client_id, code, verifier = _complete_authorize_via_sso(mcp_client)

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
) -> None:
    """A wrong verifier MUST NOT mint a token (PKCE replay defence)."""
    client_id, code, _ = _complete_authorize_via_sso(mcp_client)

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
) -> None:
    """Code is one-shot: first exchange succeeds, second 400s."""
    client_id, code, verifier = _complete_authorize_via_sso(mcp_client)

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
) -> None:
    """A refresh-token exchange returns a new access + refresh pair."""
    client_id, code, verifier = _complete_authorize_via_sso(mcp_client)

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
# /_health probe — used by docker-compose + nginx
# ───────────────────────────────────────────────────────────────────────────


def test_health_endpoint_reports_spike_mode_flag(
    mcp_client: TestClient,
    spike_env: None,
) -> None:
    """Ops observability — spike-mode is visible from /_health."""
    response = mcp_client.get("/_health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["spike_mode"] is True


def test_health_endpoint_reports_spike_mode_off_by_default(
    mcp_client: TestClient,
) -> None:
    """The production posture — /_health reports spike_mode: false."""
    response = mcp_client.get("/_health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["spike_mode"] is False
