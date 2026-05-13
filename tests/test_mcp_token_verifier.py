# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM1 — Token-verifier tests for the MCP server.

The verifier is the hot-path validator the MCP SDK calls on every
JSON-RPC request. These tests exercise:

  - happy-path SHA-256 lookup → AccessToken with identity scopes
    packed into ``scopes``.
  - audience-binding rejection: a row whose ``resource`` column
    doesn't match ``config.resource_indicator()`` returns None.
  - expired-row rejection.
  - the round-trip from packed-scopes to McpIdentity.
  - the in-memory cache: a second hit doesn't re-hit Postgres.

We seed rows directly via raw SQL so we don't need to round-trip
through /authorize → /token; the verifier's contract is "given a
row, here's the AccessToken" and the OAuth tests cover the row
creation path.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest
from sqlalchemy import text as sql_text


@pytest.fixture
def mcp_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the resource indicator so audience-binding tests are deterministic."""
    monkeypatch.setenv("MCP_SERVER_URL", "http://localhost:8002")
    yield


@pytest.fixture
def clean_mcp_tokens() -> Iterator[None]:
    from mcp_server.auth.token_verifier import reset_cache_for_test
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM mcp_access_tokens"))
        conn.execute(sql_text("DELETE FROM mcp_oauth_clients"))
    reset_cache_for_test()
    yield
    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM mcp_access_tokens"))
        conn.execute(sql_text("DELETE FROM mcp_oauth_clients"))
    reset_cache_for_test()


def _seed_client(client_id: str = "client-for-verifier-test") -> str:
    """INSERT a DCR row so the FK on mcp_access_tokens.client_id holds."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO mcp_oauth_clients (
                    client_id, client_secret_hash, client_name,
                    redirect_uris, grant_types, response_types,
                    token_endpoint_auth_method
                ) VALUES (
                    :cid, 'dummy-hash', 'Test Verifier Client',
                    ARRAY['https://claude.ai/api/mcp/auth_callback'],
                    ARRAY['authorization_code'], ARRAY['code'], 'none'
                )
                ON CONFLICT DO NOTHING
                """
            ),
            {"cid": client_id},
        )
    return client_id


def _seed_token(
    *,
    raw_token: str,
    tenant_id: str = "tnt_us_verifier_test",
    user_id: str = "user-verifier-test",
    user_email: str = "verifier-test@example.com",
    resource: str = "http://localhost:8002",
    expires_in_seconds: int = 3600,
    revoked: bool = False,
) -> str:
    """INSERT a token row and return the SHA-256 hash of raw_token."""
    from vargate_telemetry.db import engine

    client_id = _seed_client()
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=expires_in_seconds
    )
    revoked_at = (
        datetime.now(timezone.utc) - timedelta(minutes=1) if revoked else None
    )

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO mcp_access_tokens (
                    token_hash, client_id, tenant_id, user_id,
                    user_email, resource, scopes, expires_at,
                    revoked_at, refresh_token_hash
                ) VALUES (
                    :th, :cid, :t, :u, :e, :r, :sc, :exp, :rv, :rh
                )
                """
            ),
            {
                "th": token_hash,
                "cid": client_id,
                "t": tenant_id,
                "u": user_id,
                "e": user_email,
                "r": resource,
                "sc": ["log_interaction"],
                "exp": expires_at,
                "rv": revoked_at,
                "rh": hashlib.sha256(b"refresh-" + raw_token.encode()).hexdigest(),
            },
        )
    return token_hash


# ───────────────────────────────────────────────────────────────────────────
# Happy path + identity packing
# ───────────────────────────────────────────────────────────────────────────


def test_verifier_returns_access_token_with_packed_identity(
    mcp_env: None,
    clean_mcp_tokens: None,
) -> None:
    """The verifier packs tenant_id/user_id/user_email into AccessToken.scopes.

    The MCP SDK's AccessToken doesn't have native fields for these, so we
    smuggle them through ``scopes`` with ``ogma:*=`` markers. Tool
    handlers decode via identity_from_access_token().
    """
    from mcp_server.auth.token_verifier import (
        OgmaMcpTokenVerifier,
        identity_from_access_token,
    )

    raw = "test-token-" + secrets.token_urlsafe(16)
    _seed_token(raw_token=raw)

    verifier = OgmaMcpTokenVerifier()
    result = asyncio.run(verifier.verify_token(raw))

    assert result is not None
    assert result.client_id == "client-for-verifier-test"
    # Identity should round-trip cleanly.
    identity = identity_from_access_token(result)
    assert identity is not None
    assert identity.tenant_id == "tnt_us_verifier_test"
    assert identity.user_id == "user-verifier-test"
    assert identity.user_email == "verifier-test@example.com"
    # Functional scope still present alongside identity markers.
    assert "log_interaction" in result.scopes


def test_verifier_rejects_audience_mismatch(
    mcp_env: None,
    clean_mcp_tokens: None,
) -> None:
    """RFC 8707 — a row whose `resource` doesn't match server_url() is rejected.

    This is the defence against a main-Ogma JWT being replayed at
    the MCP surface. The token may otherwise be perfectly valid,
    but if it was minted for a different audience, the verifier
    must NOT honor it.
    """
    from mcp_server.auth.token_verifier import OgmaMcpTokenVerifier

    raw = "wrong-audience-" + secrets.token_urlsafe(16)
    _seed_token(
        raw_token=raw,
        resource="https://ogma.vargate.ai",  # main app, not MCP
    )

    verifier = OgmaMcpTokenVerifier()
    result = asyncio.run(verifier.verify_token(raw))

    # The verifier protocol uses None to signal "no valid identity".
    assert result is None


def test_verifier_rejects_expired_token(
    mcp_env: None,
    clean_mcp_tokens: None,
) -> None:
    """The SQL ``expires_at > now()`` filter excludes the row.

    Seed with a negative TTL so the row is born expired — the lookup
    returns no rows and the verifier 401s.
    """
    from mcp_server.auth.token_verifier import OgmaMcpTokenVerifier

    raw = "expired-token-" + secrets.token_urlsafe(16)
    _seed_token(raw_token=raw, expires_in_seconds=-60)

    verifier = OgmaMcpTokenVerifier()
    result = asyncio.run(verifier.verify_token(raw))
    assert result is None


def test_verifier_rejects_revoked_token(
    mcp_env: None,
    clean_mcp_tokens: None,
) -> None:
    """``revoked_at IS NULL`` filters out rows the founder marked revoked.

    Revocation is the manual kill-switch path; today there's no
    automated route to flip it, but the schema + filter exist so the
    op is possible.
    """
    from mcp_server.auth.token_verifier import OgmaMcpTokenVerifier

    raw = "revoked-token-" + secrets.token_urlsafe(16)
    _seed_token(raw_token=raw, revoked=True)

    verifier = OgmaMcpTokenVerifier()
    result = asyncio.run(verifier.verify_token(raw))
    assert result is None


def test_verifier_returns_none_for_unknown_token(
    mcp_env: None,
    clean_mcp_tokens: None,
) -> None:
    """A bearer that was never minted MUST NOT validate.

    Hashing a random string in this test reaches a no-row DB result;
    nothing fishy should happen on the way back.
    """
    from mcp_server.auth.token_verifier import OgmaMcpTokenVerifier

    verifier = OgmaMcpTokenVerifier()
    result = asyncio.run(
        verifier.verify_token("never-issued-" + secrets.token_urlsafe(24))
    )
    assert result is None


def test_verifier_caches_repeat_lookups(
    mcp_env: None,
    clean_mcp_tokens: None,
) -> None:
    """A warm-cache hit must NOT round-trip to Postgres.

    Pop the DB row after the first lookup; the second call must
    still succeed iff the cache is doing its job.
    """
    from mcp_server.auth.token_verifier import OgmaMcpTokenVerifier
    from vargate_telemetry.db import engine

    raw = "cached-" + secrets.token_urlsafe(16)
    token_hash = _seed_token(raw_token=raw)

    verifier = OgmaMcpTokenVerifier()
    first = asyncio.run(verifier.verify_token(raw))
    assert first is not None

    with engine.begin() as conn:
        conn.execute(
            sql_text("DELETE FROM mcp_access_tokens WHERE token_hash = :h"),
            {"h": token_hash},
        )

    second = asyncio.run(verifier.verify_token(raw))
    assert second is not None  # served from cache, not DB
    assert second.scopes == first.scopes


def test_identity_decoder_returns_none_on_missing_marker() -> None:
    """If the verifier ever shipped only 2 of 3 markers, the tool handler
    must fail loud — not silently accept a half-identity."""
    from mcp.server.auth.provider import AccessToken
    from mcp_server.auth.token_verifier import identity_from_access_token

    half = AccessToken(
        token="fake",
        client_id="cid",
        scopes=[
            "log_interaction",
            "ogma:tenant_id=tnt_us_x",
            "ogma:user_id=u1",
            # NOTE: no ogma:user_email — this is the bug we want
            # the decoder to surface.
        ],
        expires_at=int(datetime.now(timezone.utc).timestamp()) + 3600,
        resource="http://localhost:8002",
    )
    assert identity_from_access_token(half) is None
