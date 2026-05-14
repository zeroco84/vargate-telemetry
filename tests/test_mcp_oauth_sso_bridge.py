# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM2 Phase C1 — /authorize redirect-to-bridge tests.

When the MCP server's /authorize is called WITHOUT spike-mode
(the production path), it now:

  - Validates the OAuth-protocol inputs (response_type, PKCE,
    redirect_uri, client registered) — same as TM1.
  - Generates a fresh ``mcp_state`` token.
  - Persists the pre-redirect OAuth state in Redis keyed by that
    token (10-min TTL).
  - 302-redirects the user-browser to
    ``<OGMA_BRIDGE_URL>?state=<mcp_state>&return=<callback_url>``.

The bridge eventually 302s back to /authorize/callback (Phase C2)
which claims the state and reaches the same code-mint shape that
the spike branch produces.

These tests are isolated from the TM1 spike-flow tests in
``test_mcp_oauth.py`` — those will be reorganized in Phase C5.
Here we exercise the NEW production path explicitly.
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text


@pytest.fixture
def mcp_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build a TestClient with spike-mode OFF (so the production path runs).

    Pins MCP_SERVER_URL so resource-indicator + callback-URL assertions
    are deterministic — without this the loader picks up the
    default http://localhost:8001 from config.server_url().
    """
    monkeypatch.delenv("MCP_SPIKE_MODE", raising=False)
    monkeypatch.setenv("MCP_SERVER_URL", "http://localhost:8002")
    from mcp_server.main import app

    return TestClient(app)


@pytest.fixture
def clean_oauth_state() -> None:
    """Wipe Redis OAuth state + DB OAuth clients before/after each test."""
    from mcp_server.auth import oauth_state
    from mcp_server.auth.oauth_routes import reset_stores_for_test
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM mcp_access_tokens"))
        conn.execute(sql_text("DELETE FROM mcp_oauth_clients"))
    reset_stores_for_test()
    oauth_state.reset_for_test()
    yield
    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM mcp_access_tokens"))
        conn.execute(sql_text("DELETE FROM mcp_oauth_clients"))
    reset_stores_for_test()
    oauth_state.reset_for_test()


def _register_client(
    mcp_client: TestClient,
    redirect_uri: str = "https://claude.ai/api/mcp/auth_callback",
) -> str:
    response = mcp_client.post(
        "/register",
        json={
            "client_name": "Test Client",
            "redirect_uris": [redirect_uri],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["client_id"]


def _valid_authorize_params(client_id: str, **overrides) -> dict:
    """A baseline-valid set of /authorize query params."""
    params = {
        "client_id": client_id,
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "response_type": "code",
        "code_challenge": "abc-test-challenge-S256-format",
        "code_challenge_method": "S256",
        "state": "claude-state-xyz",
    }
    params.update(overrides)
    return params


# ───────────────────────────────────────────────────────────────────────────
# Happy path — redirect to bridge
# ───────────────────────────────────────────────────────────────────────────


def test_authorize_redirects_to_ogma_bridge(
    mcp_client: TestClient,
    clean_oauth_state: None,
) -> None:
    """No spike mode → 302 to <OGMA_BRIDGE_URL> with state + return params."""
    client_id = _register_client(mcp_client)
    response = mcp_client.get(
        "/authorize",
        params=_valid_authorize_params(client_id),
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    parsed = urlparse(location)
    # Default bridge URL is the prod Ogma host.
    assert (
        f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        == "https://ogma.vargate.ai/auth/mcp-bridge"
    )
    qs = parse_qs(parsed.query)
    assert "state" in qs and len(qs["state"][0]) >= 32  # mcp_state token
    # The fixture pins MCP_SERVER_URL=http://localhost:8002, so the
    # callback URL derives from that. Production envs override via
    # MCP_AUTHORIZE_CALLBACK_URL to https://mcp.ogma.vargate.ai/...
    assert qs["return"][0] == "http://localhost:8002/authorize/callback"


def test_authorize_persists_oauth_state_in_redis(
    mcp_client: TestClient,
    clean_oauth_state: None,
) -> None:
    """The mcp_state in the redirect URL keys a Redis entry with the OAuth payload."""
    from mcp_server.auth import oauth_state

    client_id = _register_client(mcp_client)
    response = mcp_client.get(
        "/authorize",
        params=_valid_authorize_params(
            client_id,
            scope="log_interaction",
            state="claude-original-state-roundtrip",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 302
    mcp_state = parse_qs(urlparse(response.headers["location"]).query)[
        "state"
    ][0]

    # The state should be claimable + carry the original OAuth params.
    payload = oauth_state.claim(mcp_state)
    assert payload is not None
    assert payload["client_id"] == client_id
    assert payload["redirect_uri"] == (
        "https://claude.ai/api/mcp/auth_callback"
    )
    assert payload["code_challenge"] == "abc-test-challenge-S256-format"
    assert payload["code_challenge_method"] == "S256"
    assert payload["scope"] == "log_interaction"
    assert payload["resource"] == "http://localhost:8002"
    assert payload["claude_state"] == "claude-original-state-roundtrip"


def test_authorize_oauth_state_is_one_shot(
    mcp_client: TestClient,
    clean_oauth_state: None,
) -> None:
    """A second claim of the same mcp_state returns None (replay defence)."""
    from mcp_server.auth import oauth_state

    client_id = _register_client(mcp_client)
    response = mcp_client.get(
        "/authorize",
        params=_valid_authorize_params(client_id),
        follow_redirects=False,
    )
    mcp_state = parse_qs(urlparse(response.headers["location"]).query)[
        "state"
    ][0]

    first = oauth_state.claim(mcp_state)
    second = oauth_state.claim(mcp_state)
    assert first is not None
    assert second is None


def test_authorize_generates_unique_mcp_state_per_request(
    mcp_client: TestClient,
    clean_oauth_state: None,
) -> None:
    """Two /authorize calls produce two different mcp_state tokens."""
    client_id = _register_client(mcp_client)
    r1 = mcp_client.get(
        "/authorize",
        params=_valid_authorize_params(client_id),
        follow_redirects=False,
    )
    r2 = mcp_client.get(
        "/authorize",
        params=_valid_authorize_params(client_id),
        follow_redirects=False,
    )
    s1 = parse_qs(urlparse(r1.headers["location"]).query)["state"][0]
    s2 = parse_qs(urlparse(r2.headers["location"]).query)["state"][0]
    assert s1 != s2


def test_authorize_preserves_claude_state(
    mcp_client: TestClient,
    clean_oauth_state: None,
) -> None:
    """The original `state` query param round-trips into the stored OAuth state."""
    from mcp_server.auth import oauth_state

    client_id = _register_client(mcp_client)
    response = mcp_client.get(
        "/authorize",
        params=_valid_authorize_params(client_id, state="UNIQUE-CLAUDE-STATE"),
        follow_redirects=False,
    )
    mcp_state = parse_qs(urlparse(response.headers["location"]).query)[
        "state"
    ][0]
    payload = oauth_state.claim(mcp_state)
    assert payload["claude_state"] == "UNIQUE-CLAUDE-STATE"


# ───────────────────────────────────────────────────────────────────────────
# Validation still runs BEFORE the redirect
# ───────────────────────────────────────────────────────────────────────────


def test_invalid_pkce_method_400s_before_redirect(
    mcp_client: TestClient,
    clean_oauth_state: None,
) -> None:
    """Non-S256 PKCE rejected — no Redis state written, no redirect."""
    from mcp_server.auth import oauth_state

    client_id = _register_client(mcp_client)
    response = mcp_client.get(
        "/authorize",
        params=_valid_authorize_params(
            client_id, code_challenge_method="plain"
        ),
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_request"


def test_unknown_client_400s_before_redirect(
    mcp_client: TestClient,
    clean_oauth_state: None,
) -> None:
    """A bogus client_id is rejected before we touch Redis."""
    response = mcp_client.get(
        "/authorize",
        params=_valid_authorize_params("not-a-real-client"),
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_client"


def test_off_allowlist_redirect_uri_400s_before_redirect(
    mcp_client: TestClient,
    clean_oauth_state: None,
) -> None:
    """An attacker-controlled redirect_uri can't get us to the bridge."""
    client_id = _register_client(mcp_client)
    response = mcp_client.get(
        "/authorize",
        params=_valid_authorize_params(
            client_id, redirect_uri="https://evil.example.com/cb"
        ),
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_redirect_uri"


# ───────────────────────────────────────────────────────────────────────────
# Bridge URL is env-driven (dev / staging override)
# ───────────────────────────────────────────────────────────────────────────


def test_bridge_url_respects_env_override(
    mcp_client: TestClient,
    clean_oauth_state: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OGMA_BRIDGE_URL env override changes where /authorize redirects to."""
    monkeypatch.setenv(
        "OGMA_BRIDGE_URL", "https://staging.ogma.vargate.ai/auth/mcp-bridge"
    )
    client_id = _register_client(mcp_client)
    response = mcp_client.get(
        "/authorize",
        params=_valid_authorize_params(client_id),
        follow_redirects=False,
    )
    assert response.status_code == 302
    parsed = urlparse(response.headers["location"])
    assert parsed.netloc == "staging.ogma.vargate.ai"
    assert parsed.path == "/auth/mcp-bridge"


def test_callback_url_respects_env_override(
    mcp_client: TestClient,
    clean_oauth_state: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP_AUTHORIZE_CALLBACK_URL changes the `return` value in the redirect."""
    monkeypatch.setenv(
        "MCP_AUTHORIZE_CALLBACK_URL",
        "https://staging-mcp.ogma.vargate.ai/authorize/callback",
    )
    client_id = _register_client(mcp_client)
    response = mcp_client.get(
        "/authorize",
        params=_valid_authorize_params(client_id),
        follow_redirects=False,
    )
    qs = parse_qs(urlparse(response.headers["location"]).query)
    assert (
        qs["return"][0]
        == "https://staging-mcp.ogma.vargate.ai/authorize/callback"
    )
