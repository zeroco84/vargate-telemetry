# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""TM2 Phase B2 — /auth/mcp-bridge endpoint tests.

Browser-facing redirect endpoint. Behaviors covered:

  - Signed-in + valid return URL → 302 to <return>?bridge_token=<JWT>;
    JWT round-trips the identity claims via the bridge_keys
    verifier.
  - Signed-out → 302 to /onboarding/sso?next_url=<bridge URL>.
  - Signed-in but no tenant_id (mid-onboarding) → 302 to
    /onboarding/select-region.
  - Invalid `return` URL → 400 invalid_return_url.
  - Missing query params → 422 (FastAPI validation).
  - Tampered session cookie → treated as signed-out (don't 500).
  - Configurable allowlist via OGMA_MCP_BRIDGE_ALLOWED_RETURN_URLS.

The TestClient doesn't follow redirects, so we assert directly on
the 302 + Location header. URL parsing is via stdlib urllib.parse
so we don't drift from the implementation's encoding choices.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from fastapi.testclient import TestClient

from vargate_telemetry.auth.jwt import (
    SESSION_COOKIE_NAME,
    issue_session_jwt,
)


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


def _signed_in_cookie(
    *,
    user_id: str = "user-bridge-test",
    email: str = "bridge-test@example.com",
    tenant_id: str | None = "tnt_us_bridge_test",
) -> dict[str, str]:
    """Build a session cookie for a signed-in user in the test session."""
    token = issue_session_jwt(
        user_id=user_id,
        email=email,
        sso_provider="google",
        tenant_id=tenant_id,
    )
    return {SESSION_COOKIE_NAME: token}


# ───────────────────────────────────────────────────────────────────────────
# Signed-in happy path
# ───────────────────────────────────────────────────────────────────────────


def test_signed_in_user_gets_redirect_with_bridge_token(
    client: TestClient,
) -> None:
    """Happy path — 302 back to the MCP callback with `bridge_token`."""
    response = client.get(
        "/auth/mcp-bridge",
        params={
            "state": "mcp-state-abc",
            "return": "https://mcp.ogma.vargate.ai/authorize/callback",
        },
        cookies=_signed_in_cookie(),
        follow_redirects=False,
    )
    assert response.status_code == 302
    location = response.headers["location"]
    parsed = urlparse(location)
    assert (
        f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        == "https://mcp.ogma.vargate.ai/authorize/callback"
    )
    qs = parse_qs(parsed.query)
    assert "bridge_token" in qs
    assert len(qs["bridge_token"]) == 1


def test_bridge_token_carries_identity_claims(client: TestClient) -> None:
    """The minted JWT decodes back to the signed-in user's identity."""
    from vargate_telemetry.auth import bridge_keys

    response = client.get(
        "/auth/mcp-bridge",
        params={
            "state": "round-trip-state",
            "return": "https://mcp.ogma.vargate.ai/authorize/callback",
        },
        cookies=_signed_in_cookie(
            user_id="user-claims-test",
            email="claims-test@example.com",
            tenant_id="tnt_us_claims_test",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 302
    location = response.headers["location"]
    qs = parse_qs(urlparse(location).query)
    token = qs["bridge_token"][0]

    bridge_keys.reset_cache_for_test()
    claims = bridge_keys.verify_bridge_token(token)
    assert claims["tenant_id"] == "tnt_us_claims_test"
    assert claims["user_id"] == "user-claims-test"
    assert claims["user_email"] == "claims-test@example.com"
    assert claims["mcp_state"] == "round-trip-state"
    assert claims["aud"] == "mcp-bridge"
    assert claims["iss"] == "ogma.vargate.ai"


# ───────────────────────────────────────────────────────────────────────────
# Signed-out / mid-onboarding branches
# ───────────────────────────────────────────────────────────────────────────


def test_signed_out_user_gets_redirect_to_sso(client: TestClient) -> None:
    """No session cookie → 302 to /onboarding/sso with next_url query."""
    response = client.get(
        "/auth/mcp-bridge",
        params={
            "state": "anon-state",
            "return": "https://mcp.ogma.vargate.ai/authorize/callback",
        },
        # No cookies sent
        follow_redirects=False,
    )
    assert response.status_code == 302
    location = response.headers["location"]
    parsed = urlparse(location)
    assert parsed.path == "/onboarding/sso"
    qs = parse_qs(parsed.query)
    assert "next_url" in qs
    # next_url contains the bridge URL with both query params
    next_url = qs["next_url"][0]
    assert "/auth/mcp-bridge" in next_url
    assert "state=anon-state" in next_url
    assert "return=" in next_url


def test_invalid_session_cookie_treated_as_signed_out(
    client: TestClient,
) -> None:
    """A garbled or expired cookie should NOT 500 — bounce to SSO."""
    response = client.get(
        "/auth/mcp-bridge",
        params={
            "state": "garbled-state",
            "return": "https://mcp.ogma.vargate.ai/authorize/callback",
        },
        cookies={SESSION_COOKIE_NAME: "not.a.valid.jwt"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "/onboarding/sso" in response.headers["location"]


def test_signed_in_no_tenant_redirects_to_select_region(
    client: TestClient,
) -> None:
    """Mid-onboarding user (signed in, tenant_id=None) goes to select-region."""
    response = client.get(
        "/auth/mcp-bridge",
        params={
            "state": "midflight-state",
            "return": "https://mcp.ogma.vargate.ai/authorize/callback",
        },
        cookies=_signed_in_cookie(tenant_id=None),
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "/onboarding/select-region" in response.headers["location"]


# ───────────────────────────────────────────────────────────────────────────
# Return-URL allowlist (security floor)
# ───────────────────────────────────────────────────────────────────────────


def test_return_url_off_allowlist_returns_400(client: TestClient) -> None:
    """An attacker-controlled `return` must NOT redirect anywhere."""
    response = client.get(
        "/auth/mcp-bridge",
        params={
            "state": "attack-state",
            "return": "https://evil.example.com/steal",
        },
        cookies=_signed_in_cookie(),
        follow_redirects=False,
    )
    assert response.status_code == 400
    body = response.json()["detail"]
    assert body["code"] == "invalid_return_url"
    # Must NOT reflect the bad URL in the response body — that would
    # be a small XSS amplification if a future client renders it.
    assert "evil.example.com" not in response.text


def test_return_url_must_match_exactly(client: TestClient) -> None:
    """Substring or prefix matches don't count — exact-string only."""
    response = client.get(
        "/auth/mcp-bridge",
        params={
            "state": "fuzz",
            "return": (
                "https://mcp.ogma.vargate.ai/authorize/callback/extra"
            ),
        },
        cookies=_signed_in_cookie(),
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_return_url_allowlist_is_env_driven(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dev override (e.g. localhost callback) is honored when set."""
    monkeypatch.setenv(
        "OGMA_MCP_BRIDGE_ALLOWED_RETURN_URLS",
        (
            "https://mcp.ogma.vargate.ai/authorize/callback,"
            "http://localhost:6274/oauth/callback"
        ),
    )
    response = client.get(
        "/auth/mcp-bridge",
        params={
            "state": "dev-state",
            "return": "http://localhost:6274/oauth/callback",
        },
        cookies=_signed_in_cookie(),
        follow_redirects=False,
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("http://localhost:6274/oauth/callback?")


# ───────────────────────────────────────────────────────────────────────────
# Missing-query-param validation
# ───────────────────────────────────────────────────────────────────────────


def test_missing_state_param_returns_422(client: TestClient) -> None:
    response = client.get(
        "/auth/mcp-bridge",
        params={
            "return": "https://mcp.ogma.vargate.ai/authorize/callback",
        },
        cookies=_signed_in_cookie(),
        follow_redirects=False,
    )
    assert response.status_code == 422


def test_missing_return_param_returns_422(client: TestClient) -> None:
    response = client.get(
        "/auth/mcp-bridge",
        params={"state": "x"},
        cookies=_signed_in_cookie(),
        follow_redirects=False,
    )
    assert response.status_code == 422
