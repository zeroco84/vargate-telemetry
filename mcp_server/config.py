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
    False, /authorize returns 501. When True, /authorize uses the
    static MCP_TEST_IDENTITY_* env vars to issue an auth code
    without an SSO redirect.
    """
    return os.environ.get("MCP_SPIKE_MODE", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
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
