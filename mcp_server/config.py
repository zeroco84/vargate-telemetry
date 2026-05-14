# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM1 — MCP server config.

Centralizes the env-driven knobs so any caller (FastAPI routes,
Celery tasks, the FastMCP server) reads from one place.

SPIKE-ONLY KNOBS
================
``MCP_SPIKE_MODE`` and ``MCP_TEST_IDENTITY_*`` are spike-only
shortcuts that bypass the full SSO bridge described in the TM1
spec §10. They exist so the feasibility test in §6 can run before
the bridge is built. **Production builds must NOT set
MCP_SPIKE_MODE.** When the var is unset, the /authorize endpoint
returns 501 Not Implemented — forcing TM2 to either build the real
bridge or consciously re-enable spike mode.

The spike mode is also LOUD: every /authorize call in spike mode
emits a WARNING-level log so anyone reading server logs in the
next two months will see it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


# ───────────────────────────────────────────────────────────────────────────
# Resource identity
# ───────────────────────────────────────────────────────────────────────────


def server_url() -> str:
    """Canonical MCP server base URL.

    Used as the OAuth issuer, the resource indicator (RFC 8707
    audience), and the absolute URL of the well-known metadata
    endpoints.

    In production: ``https://mcp.ogma.vargate.ai``.
    Local dev: defaults to ``http://localhost:8001`` — overridable.
    """
    return os.environ.get("MCP_SERVER_URL", "http://localhost:8001")


# ───────────────────────────────────────────────────────────────────────────
# TM2 — SSO bridge wiring
# ───────────────────────────────────────────────────────────────────────────


def ogma_bridge_url() -> str:
    """The /auth/mcp-bridge endpoint on Ogma's gateway.

    ``/authorize`` 302s the user-browser here with ``state=<mcp_state>&
    return=<authorize_callback_url>`` query parameters. The bridge
    JWT it eventually returns is verified using the public key
    fetched from ``OGMA_PUBLIC_KEY_URL``.

    Production default: ``https://ogma.vargate.ai/auth/mcp-bridge``.
    Override via env for dev / staging.
    """
    return os.environ.get(
        "OGMA_BRIDGE_URL", "https://ogma.vargate.ai/auth/mcp-bridge"
    )


def authorize_callback_url() -> str:
    """The MCP server's ``/authorize/callback`` URL (the ``return`` value).

    Sent to the SSO bridge as the ``return`` query parameter. The
    bridge validates this against its own allowlist
    (``OGMA_MCP_BRIDGE_ALLOWED_RETURN_URLS`` on the gateway side)
    before signing a bridge JWT.

    Defaults to ``<server_url()>/authorize/callback`` so it tracks
    server_url(). Independent override exists in case nginx routing
    ever wants to put the callback at a different path.
    """
    explicit = os.environ.get("MCP_AUTHORIZE_CALLBACK_URL")
    if explicit:
        return explicit
    return f"{server_url().rstrip('/')}/authorize/callback"


def ogma_public_key_url() -> str:
    """Where the MCP server fetches the bridge JWT verification JWK.

    Used at MCP-server startup (Phase C3) and by the daily refresh
    Celery beat task (Phase C4).
    """
    return os.environ.get(
        "OGMA_PUBLIC_KEY_URL",
        "https://ogma.vargate.ai/.well-known/ogma-public-key.json",
    )


def resource_indicator() -> str:
    """RFC 8707 audience value embedded in every issued access token.

    Identical to ``server_url()`` for now. Kept as a separate
    function because Anthropic's resource indicator implementation
    may evolve and we want one place to change.
    """
    return server_url()


# ───────────────────────────────────────────────────────────────────────────
# Spike-mode gate
# ───────────────────────────────────────────────────────────────────────────


def spike_mode_enabled() -> bool:
    """True iff MCP_SPIKE_MODE is set to a truthy value.

    The /authorize endpoint reads this once per request. When
    False, /authorize returns 501 (TM1) or redirects to the SSO
    bridge (TM2 Phase C). When True, /authorize uses the static
    MCP_TEST_IDENTITY_* env vars to issue an auth code without an
    SSO redirect.

    TM2 Phase A3: production deploys MUST NOT set MCP_SPIKE_MODE.
    :func:`assert_spike_mode_safe` is called at startup and refuses
    to boot if the var is set without the test-bypass override.
    """
    return os.environ.get("MCP_SPIKE_MODE", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _test_bypass_enabled() -> bool:
    """True iff the test-suite has opted out of the production guard.

    The pytest conftest sets ``MCP_ALLOW_SPIKE_MODE_FOR_TESTING=1``
    before any test module imports the server, so unit tests that
    need a deterministic identity (without round-tripping a full
    SSO flow) can still set ``MCP_SPIKE_MODE=true`` in a monkeypatch
    block.

    Production must NOT set this variable. Its presence in a
    production env is itself a bug.
    """
    return os.environ.get(
        "MCP_ALLOW_SPIKE_MODE_FOR_TESTING", ""
    ).lower() in ("1", "true", "yes", "on")


def assert_spike_mode_safe() -> None:
    """Startup-time guard against accidentally shipping spike mode (TM2).

    Called from ``mcp_server/main.py`` at module import. Refuses to
    let the server boot if ``MCP_SPIKE_MODE`` is set in any form
    AND ``MCP_ALLOW_SPIKE_MODE_FOR_TESTING`` is NOT set.

    The CLAUDE.md rule "Spike mode is dead" is enforced here. If
    you genuinely need spike mode for a test, the bypass env var
    is the documented escape hatch — but it must never appear in
    production environments. Audit your env files if you see it.
    """
    if not spike_mode_enabled():
        return
    if _test_bypass_enabled():
        # Tests explicitly opted in. The unit-test suite uses spike
        # mode for fixtures that need a static identity without
        # running the full SSO bridge round-trip.
        return

    raise RuntimeError(
        "CRITICAL: MCP_SPIKE_MODE is set in an environment that "
        "did not declare MCP_ALLOW_SPIKE_MODE_FOR_TESTING. Spike "
        "mode is a TM1-only shortcut and was removed from the "
        "production code path in TM2. Either:\n"
        "  - Unset MCP_SPIKE_MODE (the production path), or\n"
        "  - Set MCP_ALLOW_SPIKE_MODE_FOR_TESTING=1 if you are "
        "intentionally running the test-suite scenarios.\n"
        "If this message appears in a production deploy log, "
        "you have a misconfigured .env file — fix it before "
        "letting the server bind to a port."
    )


@dataclass(frozen=True)
class TestIdentity:
    """The static identity used when MCP_SPIKE_MODE is on.

    Comes from three env vars so the spike can target a real Ogma
    tenant in the founder's prod DB without code changes.
    """

    tenant_id: str
    user_id: str
    user_email: str


def test_identity() -> Optional[TestIdentity]:
    """Read MCP_TEST_IDENTITY_{TENANT_ID,USER_ID,USER_EMAIL}.

    Returns ``None`` if any var is unset — caller must short-circuit
    (typically with 503 / "spike mode mis-configured").
    """
    tenant = os.environ.get("MCP_TEST_IDENTITY_TENANT_ID")
    user = os.environ.get("MCP_TEST_IDENTITY_USER_ID")
    email = os.environ.get("MCP_TEST_IDENTITY_USER_EMAIL")
    if not (tenant and user and email):
        return None
    return TestIdentity(
        tenant_id=tenant, user_id=user, user_email=email
    )


# ───────────────────────────────────────────────────────────────────────────
# Token TTLs
# ───────────────────────────────────────────────────────────────────────────

# 1 hour access tokens, 30 day refresh tokens — per the TM1 spec.
# Both knobs are settable via env in case the spike turns up issues
# (Claude not refreshing properly, etc.).

ACCESS_TOKEN_TTL_SECONDS = int(
    os.environ.get("MCP_ACCESS_TOKEN_TTL_SECONDS", str(60 * 60))
)
REFRESH_TOKEN_TTL_SECONDS = int(
    os.environ.get("MCP_REFRESH_TOKEN_TTL_SECONDS", str(30 * 24 * 60 * 60))
)


# ───────────────────────────────────────────────────────────────────────────
# Allowed Claude callback URLs
# ───────────────────────────────────────────────────────────────────────────
#
# Per the TM1 spec §1.2: Claude's OAuth callback URL is
# ``https://claude.ai/api/mcp/auth_callback`` — but Anthropic has
# noted ``claude.com`` may eventually replace ``claude.ai``, so
# we allowlist both. Dynamic Client Registration validates the
# Claude-supplied ``redirect_uris`` against this list.

ALLOWED_REDIRECT_URI_PREFIXES = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    # Local MCP inspector for the §6 feasibility test:
    "http://localhost:6274/oauth/callback",
    "http://localhost:6274/oauth/callback/debug",
)
