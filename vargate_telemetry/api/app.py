# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""FastAPI application instance for the Ogma gateway (T4.2).

`root_path="/api"` matches the `servers: [{url: /api}]` declaration
in `openapi/ogma-api.yaml`. In production, nginx routes
`vargate.ai/api/*` to this app and strips the `/api/` prefix
before forwarding — so the gateway sees paths like
`/auth/sso/google/callback`. The OpenAPI document FastAPI
auto-generates (and `scripts/validate_openapi.py` will diff
against the committed YAML when OGMA_OPENAPI_DIFF_ROUTES=1) lists
paths without the prefix; the prefix lives in the `servers` block.

The gateway is intentionally thin. Auth + onboarding live in
`vargate_telemetry/auth/`; this file is the wiring layer that
mounts route modules and configures CORS / docs.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from vargate_telemetry.api import auth as auth_routes


def _build_app() -> FastAPI:
    app = FastAPI(
        title="Vargate Ogma API",
        version="0.1.0",
        description=(
            "Ogma — the Vargate Telemetry product. Contract source of "
            "truth lives at `openapi/ogma-api.yaml`; this app's "
            "auto-generated OpenAPI is verified to match by "
            "`scripts/validate_openapi.py`."
        ),
        root_path="/api",
        # Disable /docs and /redoc by default — auditor-facing UX is
        # in the dashboard, not Swagger. Re-enable per-env if the
        # ops team wants the FastAPI docs surface back.
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    # The frontend runs at vargate.ai (or localhost during dev) on
    # the same origin as the gateway's /api/. Cross-origin is only
    # needed if the dashboard ever moves to a separate domain. For
    # now allow the dev vite server.
    dev_origins = os.environ.get(
        "OGMA_CORS_ORIGINS", "http://localhost:5173"
    ).split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in dev_origins if o.strip()],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(auth_routes.router)

    @app.get("/_health", include_in_schema=False)
    def _health() -> dict:
        """Liveness probe — not part of the public contract."""
        return {"status": "ok"}

    return app


app = _build_app()
