# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM2 Phase C4 — daily refresh of the bridge JWK.

The MCP server's lifespan primes the bridge_verifier cache at boot
(Phase C3) by fetching ``/.well-known/ogma-public-key.json`` from
Ogma's gateway. That gets us right at startup, but for key rotation
to actually propagate without a redeploy, the MCP server has to
re-fetch on a schedule.

This Celery beat-scheduled task re-runs the fetch every 24 hours.
The cache is updated in-place on success — pyjwt's ``decode()``
picks up the new key on its next call, no separate signal needed.

On failure, the task LOGS but does NOT raise. Rationale: the MCP
server is already running with a previously-cached JWK that's
still valid (just possibly stale). A transient gateway hiccup
during the refresh window shouldn't kick the task into Celery's
retry-and-eventually-fail path; the cache stays warm and
``/authorize/callback`` keeps working. The next scheduled run
will try again.

Operationally: if the kid in the log line changes after a refresh,
rotation has been seen. If a refresh fails for 48+ hours and the
gateway's signing key actually changed during that window, bridge
verification will start 400ing — that's the signal that something
deeper is wrong (gateway down, DNS, TLS cert renewal failed, etc.)
and is worth a page.
"""

from __future__ import annotations

import logging

from vargate_telemetry.celery_app import celery_app

from mcp_server.auth import bridge_verifier


_log = logging.getLogger(__name__)


@celery_app.task(
    name="mcp_server.tasks.refresh_bridge_jwk.refresh_bridge_jwk",
    # Retry policy: NONE. A transient failure isn't worth retrying
    # mid-window — the next 24h tick re-fetches anyway, and the
    # warm cache means the failure isn't user-visible. Setting
    # max_retries=0 keeps the worker log clean.
    max_retries=0,
)
def refresh_bridge_jwk() -> dict:
    """Re-fetch + cache the bridge JWK. Returns a small status dict."""
    try:
        jwk = bridge_verifier.fetch_and_cache_jwk()
    except Exception as exc:
        _log.warning(
            "refresh_bridge_jwk: fetch failed (keeping stale cache): "
            "%s: %s",
            type(exc).__name__,
            exc,
        )
        return {
            "refreshed": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    _log.info(
        "refresh_bridge_jwk: cached new JWK kid=%s",
        jwk.get("kid", "<unknown>"),
    )
    return {"refreshed": True, "kid": jwk.get("kid")}
