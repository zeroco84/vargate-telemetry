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

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from vargate_telemetry.api import auth as auth_routes
from vargate_telemetry.api import budgets as budgets_routes
from vargate_telemetry.api import compliance_key as compliance_key_routes
from vargate_telemetry.api import mcp_bridge as mcp_bridge_routes
from vargate_telemetry.api import onboarding as onboarding_routes
from vargate_telemetry.api import sessions as sessions_routes
from vargate_telemetry.api import usage as usage_routes
from vargate_telemetry.api import users as users_routes
from vargate_telemetry.api import well_known as well_known_routes

# Side-effect import: registers the T4.7 onboarding instruments against
# the default Prometheus registry at module-load time, so `/metrics`
# returns them on the very first scrape (no warm-up traffic needed).
# Also exposes `get_registry()` for the multi-process /metrics route
# below (T4.8.1).
import vargate_telemetry.metrics as metrics_pkg  # noqa: F401


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
    app.include_router(onboarding_routes.router)
    # TM5 T5.1: POST /onboarding/compliance-key — validate + seal a
    # Compliance Access Key, enabling content capture (admin-gated).
    app.include_router(compliance_key_routes.router)
    app.include_router(sessions_routes.router)
    app.include_router(usage_routes.router)
    # TM3 Phase B2: /api/budgets + /api/budget-alerts CRUD.
    app.include_router(budgets_routes.router)
    # TM3 Phase C2: /api/users cross-surface roster + detail + aliases.
    app.include_router(users_routes.router)
    # TM2 Phase B1: /.well-known/ogma-public-key.json — public JWK
    # the MCP server fetches at boot to verify bridge JWTs.
    app.include_router(well_known_routes.router)
    # TM2 Phase B2: /auth/mcp-bridge — Ogma SSO → MCP authorization
    # callback bridge. Browser-facing redirect endpoint.
    app.include_router(mcp_bridge_routes.router)

    @app.get("/_health", include_in_schema=False)
    def _health() -> dict:
        """Liveness probe — not part of the public contract."""
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    def _metrics() -> Response:
        """Prometheus scrape endpoint (T4.7 + T4.8.1).

        Builds a fresh registry per scrape via
        ``vargate_telemetry.metrics.get_registry()``: when
        ``PROMETHEUS_MULTIPROC_DIR`` is set (the compose / prod path),
        the returned registry is a ``CollectorRegistry`` backed by
        ``MultiProcessCollector`` — so observations emitted by the
        celery-worker / celery-beat processes show up here alongside
        the gateway's own. Without the env var (dev shell, unit
        tests) it falls back to the default in-process REGISTRY.

        Kept off the OpenAPI surface (not part of the customer-facing
        contract) and intentionally unauthenticated — ops scrape this
        from inside the network. If we ever expose this publicly, gate
        at nginx via IP allowlist or a header check.
        """
        return Response(
            content=generate_latest(metrics_pkg.get_registry()),
            media_type=CONTENT_TYPE_LATEST,
        )

    return app


app = _build_app()
