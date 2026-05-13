# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM1 — Bearer token validator for the MCP server.

Implements MCP SDK's :class:`mcp.server.auth.provider.TokenVerifier`
protocol. The /mcp endpoint calls ``verify_token(raw_bearer)`` on
every JSON-RPC request; the verifier:

  1. SHA-256 hashes the bearer.
  2. Looks the row up by hash in ``mcp_access_tokens``
     (scheduler_session_scope — no RLS gate; the row IS the
     identity lookup).
  3. Validates ``revoked_at IS NULL`` and ``expires_at > now()``.
  4. Validates ``resource`` matches the MCP server's
     RFC 8707 audience (rejects main-Ogma JWTs replayed at the
     MCP surface).
  5. Returns the MCP SDK's :class:`AccessToken` with the
     ``tenant_id``, ``user_id``, ``user_email`` packed into the
     ``scopes`` array (the MCP SDK exposes scopes to the tool
     handler via ``ctx.request_context.user.scopes``).

Performance: in-memory TTL cache on the hash so a steady-state
tool-call sequence is O(memory). Cold misses hit Postgres once
per cache TTL.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional

from mcp.server.auth.provider import AccessToken, TokenVerifier
from sqlalchemy import text as sql_text

from mcp_server import config
from vargate_telemetry.db import scheduler_session_scope


_log = logging.getLogger(__name__)


def hash_bearer(token: str) -> str:
    """SHA-256 hex digest of the bearer token (canonical column form)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ───────────────────────────────────────────────────────────────────────────
# In-memory cache
# ───────────────────────────────────────────────────────────────────────────
#
# Cache hits are sub-millisecond. The cache stores the same
# ``AccessToken`` we'd build from a DB row; entries expire at
# ``expires_at`` from the DB (so a refresh-rotated token won't
# linger past the original expiry).

_CACHE_MAX_ENTRIES = 1024


class _TokenCacheEntry:
    __slots__ = ("access_token", "expires_at_epoch")

    def __init__(self, access_token: AccessToken, expires_at_epoch: float):
        self.access_token = access_token
        self.expires_at_epoch = expires_at_epoch


_TOKEN_CACHE: dict[str, _TokenCacheEntry] = {}


def _cache_get(token_hash: str) -> Optional[AccessToken]:
    entry = _TOKEN_CACHE.get(token_hash)
    if entry is None:
        return None
    if entry.expires_at_epoch <= time.time():
        # Expired — drop the stale entry so the next call hits the DB.
        _TOKEN_CACHE.pop(token_hash, None)
        return None
    return entry.access_token


def _cache_put(
    token_hash: str, access_token: AccessToken, expires_at_epoch: float
) -> None:
    # Naive eviction — drop one arbitrary entry when full. The MCP
    # surface has bounded token cardinality (one per active Claude
    # client) so we don't need a real LRU policy.
    if len(_TOKEN_CACHE) >= _CACHE_MAX_ENTRIES:
        try:
            _TOKEN_CACHE.pop(next(iter(_TOKEN_CACHE)))
        except StopIteration:  # pragma: no cover — race-safe
            pass
    _TOKEN_CACHE[token_hash] = _TokenCacheEntry(
        access_token, expires_at_epoch
    )


def reset_cache_for_test() -> None:
    """Test hook — clear the in-memory cache between cases."""
    _TOKEN_CACHE.clear()


# ───────────────────────────────────────────────────────────────────────────
# OgmaMcpTokenVerifier
# ───────────────────────────────────────────────────────────────────────────


class OgmaMcpTokenVerifier(TokenVerifier):
    """Implements TokenVerifier against the ``mcp_access_tokens`` table.

    The MCP SDK calls ``verify_token`` from inside its ASGI
    middleware. We return ``None`` for any failure mode — the SDK
    surfaces that as 401 + WWW-Authenticate per RFC 9728.

    Identity propagation: the MCP SDK's ``AccessToken`` doesn't
    have first-class fields for our tenant_id / user_id / email.
    We pack them into ``scopes`` with prefix markers:

        scopes = ["log_interaction", "ogma:tenant_id=<t>",
                  "ogma:user_id=<u>", "ogma:user_email=<e>"]

    The tool handler reads them back out via
    :func:`identity_from_access_token`. Ugly but it's the seam the
    SDK gives us — refactor if/when MCP grows a proper auth-context
    primitive.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        token_hash = hash_bearer(token)

        cached = _cache_get(token_hash)
        if cached is not None:
            return cached

        # Cold lookup. scheduler_session_scope: no app.tenant_id —
        # the row IS the identity binding; we don't have a tenant
        # to scope by yet.
        try:
            with scheduler_session_scope() as s:
                row = s.execute(
                    sql_text(
                        """
                        SELECT
                            token_hash,
                            client_id,
                            tenant_id,
                            user_id,
                            user_email,
                            resource,
                            scopes,
                            expires_at
                        FROM mcp_access_tokens
                        WHERE token_hash = :h
                          AND revoked_at IS NULL
                          AND expires_at > now()
                        """
                    ),
                    {"h": token_hash},
                ).first()
        except Exception:  # pragma: no cover — DB blip
            _log.exception("token validator: DB lookup failed")
            return None

        if row is None:
            return None

        # Audience binding — RFC 8707. Reject any token whose
        # `resource` claim doesn't match our MCP server URL. This
        # prevents main-Ogma JWTs from being replayed here even if
        # an attacker fabricated a row with our token_hash in the
        # other DB.
        expected = config.resource_indicator()
        if row.resource != expected:
            _log.warning(
                "token validator: resource mismatch token_hash=%s "
                "got=%s expected=%s",
                token_hash[:12],
                row.resource,
                expected,
            )
            return None

        # Pack identity into scopes as documented above.
        base_scopes = list(row.scopes or ["log_interaction"])
        identity_scopes = [
            f"ogma:tenant_id={row.tenant_id}",
            f"ogma:user_id={row.user_id}",
            f"ogma:user_email={row.user_email}",
        ]

        expires_epoch = row.expires_at.timestamp()
        access_token = AccessToken(
            token=token,
            client_id=row.client_id,
            scopes=base_scopes + identity_scopes,
            expires_at=int(expires_epoch),
            resource=row.resource,
        )

        _cache_put(token_hash, access_token, expires_epoch)
        return access_token


# ───────────────────────────────────────────────────────────────────────────
# Identity extraction
# ───────────────────────────────────────────────────────────────────────────
#
# The MCP tool handler receives the AccessToken via the SDK's
# context object. The identity scopes packed above are decoded back
# into a typed structure here.


from dataclasses import dataclass


@dataclass(frozen=True)
class McpIdentity:
    tenant_id: str
    user_id: str
    user_email: str


def identity_from_access_token(
    access_token: AccessToken,
) -> Optional[McpIdentity]:
    """Decode the ``ogma:*=<value>`` scope markers back into an identity.

    Returns ``None`` if any marker is missing — the tool handler
    should treat that as a server bug (the verifier always packs
    all three) and respond with an error.
    """
    tenant_id = user_id = user_email = None
    for s in access_token.scopes:
        if s.startswith("ogma:tenant_id="):
            tenant_id = s[len("ogma:tenant_id="):]
        elif s.startswith("ogma:user_id="):
            user_id = s[len("ogma:user_id="):]
        elif s.startswith("ogma:user_email="):
            user_email = s[len("ogma:user_email="):]
    if not (tenant_id and user_id and user_email):
        return None
    return McpIdentity(
        tenant_id=tenant_id, user_id=user_id, user_email=user_email
    )
