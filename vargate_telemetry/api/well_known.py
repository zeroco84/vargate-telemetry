# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""TM2 Phase B1 — well-known endpoints.

Today's single endpoint:

  GET /.well-known/ogma-public-key.json

Serves the ECDSA P-256 public key as a JWK (RFC 7517) so the MCP
server can verify bridge JWTs signed by Ogma's gateway. Public,
no auth — the JWK only carries the public coordinates.

24-hour Cache-Control. When we rotate the keypair, we bump the
``kid`` in env, redeploy gateway, then redeploy mcp-server with a
forced public-key refresh. The 24h max-age means any in-flight
caches expire within a day without manual cache busting.

The well-known path convention is RFC 8615: well-known URIs live
at the host apex (``https://ogma.vargate.ai/.well-known/...``),
NOT under ``/api/``. Nginx maps the apex path to the gateway
container — the FastAPI app responds at the literal path
``/.well-known/ogma-public-key.json`` regardless of the
``root_path="/api"`` OpenAPI prefix.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response

from vargate_telemetry.auth import bridge_keys


_log = logging.getLogger(__name__)

router = APIRouter()


# 24h cache window — matches the daily refresh cadence in TM2 §C4
# (the MCP server runs a Celery beat task to fetch fresh).
_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60


@router.get(
    "/.well-known/ogma-public-key.json",
    include_in_schema=False,
)
def ogma_public_key(response: Response) -> dict:
    """Serve the active bridge JWT public key as a JWK.

    Shape per RFC 7518 §6.2.1 (EC) — ``{kty, crv, x, y, kid, alg, use}``.

    The endpoint is intentionally unauthenticated: the key it
    serves is the PUBLIC half of an asymmetric pair, so exposure
    is by design. Any consumer (MCP server, future federation
    partners, audit tooling) can fetch this without credentials.

    The ``Cache-Control: public, max-age=86400`` header lets
    Cloudflare and any well-behaved client cache the response for
    a day. Force-refresh on rotation by bumping ``kid`` in env and
    redeploying — the cached old-kid JWK is still valid for
    verifying tokens minted before the rotation, but new tokens
    carry the new ``kid`` so consumers know to re-fetch.
    """
    response.headers["Cache-Control"] = (
        f"public, max-age={_CACHE_MAX_AGE_SECONDS}"
    )
    return bridge_keys.public_jwk()
