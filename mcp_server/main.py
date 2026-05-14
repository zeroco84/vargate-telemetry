# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM1 — entrypoint for the MCP server process.

Composes:

- FastAPI app for the OAuth surface (metadata, DCR, /authorize,
  /token).
- FastMCP's streamable_http_app mounted at ``/mcp``.

Runs as a separate container (``mcp-server`` in docker-compose),
behind nginx at ``mcp.ogma.vargate.ai``.
"""

from __future__ import annotations
from contextlib import asynccontextmanager

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mcp_server import config
from mcp_server.auth.oauth_routes import router as oauth_router
from mcp_server.mcp.server import build_mcp_server


_log = logging.getLogger(__name__)


# TM2 Phase A3: refuse to boot if MCP_SPIKE_MODE leaks into a
# production environment. Raises before any port is bound or
# FastAPI app is constructed — uvicorn surfaces the RuntimeError
# at process start so a misconfigured .env never serves traffic.
config.assert_spike_mode_safe()


def _build_app() -> FastAPI:
    # Build the FastMCP server first — its session_manager must be
    # entered via lifespan before any /mcp request can be handled.
    mcp = build_mcp_server()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(
        title="Vargate Ogma MCP Server",
        description=(
            "MCP connector for the Ogma telemetry product. Exposes a "
            "single `log_interaction` tool that Claude calls after "
            "each conversation turn. OAuth 2.1 authorization server "
            "with Dynamic Client Registration."
        ),
        version="0.1.0-tm1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    # CORS for the OAuth metadata endpoints — Claude's web client
    # may probe them cross-origin from claude.ai / claude.com.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://claude.ai", "https://claude.com"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
    )

    # OAuth surface
    app.include_router(oauth_router)

    # Health probe — used by docker-compose healthcheck + nginx.
    @app.get("/_health", include_in_schema=False)
    def health() -> dict:
        return {
            "status": "ok",
            "spike_mode": os.environ.get(
                "MCP_SPIKE_MODE", ""
            ).lower()
            in ("1", "true", "yes", "on"),
        }

    # MCP surface — mounted at root since the MCP SDK's streamable_http_app
    # already has its routes at /mcp. Our explicit APIRoutes (above) take
    # precedence in the FastAPI route table.
    app.mount("/", mcp.streamable_http_app())

    _log.info("MCP server built; streamable_http mounted at /mcp")
    return app


app = _build_app()
