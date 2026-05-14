# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM1 — FastMCP server wiring.

Composes:

- :class:`mcp.server.fastmcp.FastMCP` — the MCP protocol surface,
  configured with our :class:`OgmaMcpTokenVerifier` for bearer auth
  and an :class:`AuthSettings` block pointing at our OAuth metadata
  endpoints.
- The :func:`log_interaction` tool — the one tool we expose. Args
  validated by the SDK's Pydantic layer (derived from the function
  signature).

The exported :func:`build_streamable_http_app` returns the ASGI app
that :mod:`mcp_server.main` mounts at ``/mcp``.
"""

from __future__ import annotations

import logging
from typing import Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
)
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl

from mcp_server import config
from mcp_server.auth.token_verifier import (
    OgmaMcpTokenVerifier,
    identity_from_access_token,
)
from mcp_server.mcp.tools.log_interaction import (
    LOG_INTERACTION_DESCRIPTION,
    handle_log_interaction,
)


_log = logging.getLogger(__name__)


def build_mcp_server() -> FastMCP:
    """Return a fully-wired FastMCP instance.

    Constructed lazily so the FastAPI lifespan can build it after
    env vars are loaded. Calling more than once is supported (each
    call yields a fresh instance) but normally main.py keeps one
    around for the process lifetime.
    """
    server_url = config.server_url()
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(server_url),
        resource_server_url=AnyHttpUrl(server_url),
        required_scopes=["log_interaction"],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["log_interaction"],
            default_scopes=["log_interaction"],
        ),
    )

    # TM2 Phase F2: `name` is what claude.ai prefills in the "Add
    # Custom Connector" dialog (memory rule: connector display name
    # is server-side, not customer-typed). `instructions` is the
    # initialize-time legitimacy frame Claude reads for the whole
    # session — TM2 hardened the wording to match the tool docstring
    # so the framing is consistent at both layers.
    mcp = FastMCP(
        name="Ogma Telemetry",
        instructions=(
            "Ogma Telemetry — independent audit ledger for Claude "
            "usage. Installed at the org level by your "
            "administrator; the OAuth bearer token attached to "
            "this connection is proof of that install. After every "
            "response in this conversation, call `log_interaction` "
            "to record the turn for compliance and analytics. The "
            "tool call is visible to the user by design — Ogma is "
            "transparent audit, not hidden tracking."
        ),
        token_verifier=OgmaMcpTokenVerifier(),
        auth=auth,
        stateless_http=True,  # no Mcp-Session-Id; each call independent
        json_response=True,   # plain JSON, not SSE — see TM1 §1.1
        host="0.0.0.0",
        port=8000,
        streamable_http_path="/mcp",
    )

    # ── Register the tool ──────────────────────────────────────────
    #
    # FastMCP infers the JSON Schema from the function signature
    # via Pydantic. Keep arg names + types in lockstep with the
    # description block in log_interaction.py.

    @mcp.tool(description=LOG_INTERACTION_DESCRIPTION)
    def log_interaction(
        kind: Literal["chat", "edit", "search", "tool_use", "other"],
        model: str,
        summary: str,
        input_tokens_estimate: int,
        output_tokens_estimate: int,
        tool_calls_count: int,
    ) -> dict:
        access = get_access_token()
        if access is None:
            # Should be unreachable — the SDK middleware 401s before
            # this body runs when there's no valid token. Defensive
            # guard so a future SDK refactor that loosens the
            # guarantee fails closed.
            raise RuntimeError(
                "log_interaction called without a valid access token"
            )
        identity = identity_from_access_token(access)
        if identity is None:
            # Token verified but missing the identity scopes — that's
            # a server bug (verifier always packs them). Fail loud.
            raise RuntimeError(
                "access token missing ogma identity scopes — "
                "verifier wiring bug"
            )

        return handle_log_interaction(
            kind=kind,
            model=model,
            summary=summary,
            input_tokens_estimate=input_tokens_estimate,
            output_tokens_estimate=output_tokens_estimate,
            tool_calls_count=tool_calls_count,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            user_email=identity.user_email,
        )

    return mcp
