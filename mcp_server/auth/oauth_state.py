# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM2 Phase C1 — Redis-backed OAuth state store.

When the MCP server's ``/authorize`` redirects the user-browser
to Ogma's SSO bridge, it has to stash the original OAuth-protocol
state so the ``/authorize/callback`` handler (C2) can recover it
and mint the right auth code. The pre-redirect state is:

  - ``client_id``                  — the DCR-registered Claude client
  - ``redirect_uri``               — where Claude wants to be sent
  - ``code_challenge`` + method    — PKCE S256
  - ``scope``
  - ``resource``                   — RFC 8707 audience binding
  - ``claude_state``               — opaque value to echo back to Claude

The state is keyed by an MCP-server-generated random ``mcp_state``
token, which we hand to the bridge as the round-trip identifier.
The bridge embeds it in the bridge JWT's ``mcp_state`` claim; the
callback reads it back, looks up our state, and proceeds.

10-minute TTL — generous enough for an SSO round-trip (typical
under 30s) while bounded enough that an abandoned auth attempt
doesn't tie up Redis indefinitely. Matches the auth-code TTL.

Storage is namespace-prefixed so a stray ``KEYS *`` in production
shows it for what it is.

Format: JSON-serialized payload + Redis ``SET ... EX``. We pop on
read (read-and-delete atomic via ``GETDEL`` from Redis 6.2+) so
the state is one-shot — a replay attempt finds no row.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import redis


_log = logging.getLogger(__name__)


# 10 minutes — matches the auth-code TTL in oauth_routes.
STATE_TTL_SECONDS = 10 * 60

# Namespace so production `KEYS *` is self-documenting.
_KEY_PREFIX = "mcp:oauth_state:"


# ───────────────────────────────────────────────────────────────────────────
# Redis client — lazy singleton, same pattern as sso.py's nonce tracker.
# ───────────────────────────────────────────────────────────────────────────


_redis_client: Optional[redis.Redis] = None


def _client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(
            os.environ["REDIS_URL"], decode_responses=False
        )
    return _redis_client


def reset_for_test() -> None:
    """Wipe the OAuth state namespace + client cache. Test-only."""
    global _redis_client
    if _redis_client is not None:
        for key in _redis_client.scan_iter(f"{_KEY_PREFIX}*"):
            _redis_client.delete(key)
    _redis_client = None


# ───────────────────────────────────────────────────────────────────────────
# store + claim
# ───────────────────────────────────────────────────────────────────────────


def store(
    *,
    mcp_state: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
    scope: str,
    resource: str,
    claude_state: Optional[str],
) -> None:
    """Stash the pre-bridge OAuth state keyed by ``mcp_state``.

    Caller has just generated ``mcp_state`` as a random one-shot
    token to hand to the SSO bridge. The bridge round-trips it
    back via the bridge JWT's ``mcp_state`` claim; the callback
    handler (C2) ``claim()``s it to recover this payload.
    """
    if not mcp_state:
        raise ValueError("mcp_state required")
    payload = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "scope": scope,
        "resource": resource,
        "claude_state": claude_state,
    }
    _client().set(
        f"{_KEY_PREFIX}{mcp_state}",
        json.dumps(payload).encode("utf-8"),
        ex=STATE_TTL_SECONDS,
    )


def claim(mcp_state: str) -> Optional[dict]:
    """Atomically read + delete the state for ``mcp_state``.

    Returns ``None`` if the state is missing (expired, never set,
    or already claimed). The atomic GETDEL ensures the state is
    one-shot — a callback replay finds an empty slot and errors
    out at the OAuth layer with ``invalid_grant``.

    The MCP server's auth-code flow (TM1) takes the same
    one-shot-via-claim shape; this just mirrors it.
    """
    if not mcp_state:
        return None
    raw = _client().getdel(f"{_KEY_PREFIX}{mcp_state}")
    if raw is None:
        return None
    return json.loads(raw.decode("utf-8"))
