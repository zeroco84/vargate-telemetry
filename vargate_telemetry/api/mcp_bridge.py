# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""TM2 Phase B2 — /auth/mcp-bridge endpoint.

The second half of the SSO bridge contract (Phase B1 published
the verification key). When Claude's MCP client hits the MCP
server's ``/authorize``, the MCP server redirects the user-browser
HERE — this endpoint:

  1. Checks for an active Ogma session via the existing JWT cookie.
  2. If signed in → mints a 60-second bridge JWT carrying
     ``(tenant_id, user_id, user_email, mcp_state)`` claims,
     302-redirects to the configured callback URL with the
     ``bridge_token`` query parameter.
  3. If signed out → 302-redirects to the SPA's SSO entry at
     ``/onboarding/sso?next_url=<this URL>`` so the user can sign
     in. (SPA support for ``next_url`` lands in Phase D — until
     then a signed-out user has to manually retry the Claude
     connector flow after sign-in.)

The ``return`` query parameter is validated against an env-driven
allowlist so an attacker can't redirect the bridge JWT to a
host they control.
"""

from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from vargate_telemetry.auth import bridge_keys
from vargate_telemetry.auth.jwt import (
    SESSION_COOKIE_NAME,
    InvalidJwtError,
    decode_session_jwt,
)


_log = logging.getLogger(__name__)

router = APIRouter()


# ───────────────────────────────────────────────────────────────────────────
# Allowed return-URL list
# ───────────────────────────────────────────────────────────────────────────
#
# The `return` query parameter must match one of these exactly. An
# attacker who could supply an arbitrary `return` URL would be able
# to siphon bridge JWTs to a host they control — game over for the
# audience binding.
#
# Env var format: comma-separated. Default covers the canonical
# production callback; dev compose can override to include
# `http://localhost:6274/oauth/callback` if you ever need to point
# Claude's MCP inspector at a local Ogma.


def _allowed_returns() -> tuple[str, ...]:
    raw = os.environ.get(
        "OGMA_MCP_BRIDGE_ALLOWED_RETURN_URLS",
        "https://mcp.ogma.vargate.ai/authorize/callback",
    )
    return tuple(u.strip() for u in raw.split(",") if u.strip())


def _ogma_origin() -> str:
    """Where the SPA lives — used to build the SSO-redirect URL.

    Defaults to ``https://ogma.vargate.ai`` (prod). Dev overrides
    via env (matches the same pattern as `OGMA_OAUTH_REDIRECT_BASE`).
    """
    return os.environ.get(
        "OGMA_MCP_BRIDGE_ORIGIN", "https://ogma.vargate.ai"
    )


# ───────────────────────────────────────────────────────────────────────────
# /auth/mcp-bridge
# ───────────────────────────────────────────────────────────────────────────


@router.get(
    "/auth/mcp-bridge",
    operation_id="mcpBridge",
    tags=["auth"],
    summary="Bridge Ogma SSO → MCP server authorization callback",
    include_in_schema=False,  # consumed by the MCP server, not by SPA clients
)
def mcp_bridge(
    request: Request,
    state: str = Query(
        ...,
        min_length=1,
        max_length=256,
        description=(
            "Opaque round-trip value the MCP server uses to recover its "
            "pre-redirect state. Mirrored verbatim into the bridge JWT's "
            "`mcp_state` claim."
        ),
    ),
    return_: str = Query(
        ...,
        alias="return",
        min_length=1,
        max_length=512,
        description=(
            "Where to send the user-browser after minting the bridge JWT. "
            "Must exactly match an entry in "
            "`OGMA_MCP_BRIDGE_ALLOWED_RETURN_URLS`."
        ),
    ),
):
    """Mint a bridge JWT for the signed-in user, redirect to the MCP callback.

    Endpoint shape per TM2 §2.2. The browser-facing contract is:

      - **Signed in + valid `return`**: 302 to
        ``<return>?bridge_token=<jwt>``. The JWT carries the
        signed-in user's identity claims plus the ``mcp_state``
        the MCP server passed in.

      - **Signed out + valid `return`**: 302 to
        ``<OGMA_MCP_BRIDGE_ORIGIN>/onboarding/sso?next_url=<this URL>``
        so the SPA can bounce the user back after SSO completes.

      - **Invalid `return`**: 400 with structured error. Never
        308-redirect to an unvalidated URL — that's the attack
        the allowlist is here to prevent.
    """
    allowed = _allowed_returns()
    if return_ not in allowed:
        # Don't reflect the bad URL back into the error body — it
        # could carry attacker-controlled content. Log the violation
        # for ops triage.
        _log.warning(
            "mcp_bridge: rejected `return` URL not on allowlist "
            "(allowed=%d entries)",
            len(allowed),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_return_url",
                "message": (
                    "The `return` URL is not on the allowlist for this "
                    "Ogma deployment. Configure "
                    "OGMA_MCP_BRIDGE_ALLOWED_RETURN_URLS if you control "
                    "this MCP server, or report a bug otherwise."
                ),
            },
        )

    # Look for a valid Ogma session. We can't use Depends(current_user)
    # because that raises 401 on no-session; here we want to BRANCH
    # on signed-in vs signed-out, not 401.
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    payload = None
    if session_cookie:
        try:
            payload = decode_session_jwt(session_cookie)
        except InvalidJwtError:
            # Treat invalid cookies as signed-out — the user needs
            # to re-authenticate. Don't 500 on a stale cookie.
            payload = None

    if payload is None:
        # Signed out — bounce through SSO. SPA support for `next_url`
        # is owed in Phase D; until then, the user signs in, lands
        # on the dashboard, and has to retry the Claude connector
        # flow manually. Documented in TM2-mcp-productization-notes.
        bridge_url = (
            f"{request.url.path}?"
            + urlencode({"state": state, "return": return_})
        )
        sso_redirect = (
            f"{_ogma_origin().rstrip('/')}/onboarding/sso?"
            + urlencode({"next_url": bridge_url})
        )
        return RedirectResponse(sso_redirect, status_code=302)

    if payload.tenant_id is None:
        # Signed in but no tenant yet (mid-onboarding). The bridge
        # JWT requires a tenant_id to be useful downstream — bounce
        # to the next onboarding step.
        sso_redirect = (
            f"{_ogma_origin().rstrip('/')}/onboarding/select-region"
        )
        _log.info(
            "mcp_bridge: signed-in user %s has no tenant yet — "
            "redirecting to onboarding",
            payload.sub,
        )
        return RedirectResponse(sso_redirect, status_code=302)

    # Signed in with a tenant — mint the bridge JWT.
    token = bridge_keys.sign_bridge_token(
        tenant_id=payload.tenant_id,
        user_id=payload.sub,
        user_email=payload.email,
        mcp_state=state,
    )
    callback_url = f"{return_}?" + urlencode({"bridge_token": token})

    _log.info(
        "mcp_bridge: minted bridge JWT for tenant_id=%s user_id=%s "
        "return=%s",
        payload.tenant_id,
        payload.sub,
        return_,
    )
    return RedirectResponse(callback_url, status_code=302)
