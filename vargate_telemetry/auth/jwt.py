# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""JWT issue + verify for Ogma session cookies (T4.2).

One signing key, one algorithm (HS256), one cookie name. Keep it
boring: the JWT carries enough to identify the user and the bound
tenant; everything else is fetched per request.

Claim shape (matches `vargate-telemetry/openapi/ogma-api.yaml`'s
implicit session contract):

    {
      "sub":       "<user_id-uuid>",        # str
      "email":     "<email>",
      "sso":       "google" | "microsoft",  # the SSO provider
      "tenant_id": "<tenant_id>" | null,    # null until T4.5 binds
      "iat":       int,                     # seconds since epoch
      "exp":       int,                     # seconds since epoch
    }

The signing key comes from `JWT_SIGNING_KEY`. Ops generate a
256-bit secret (`openssl rand -hex 32`) and set it once per
environment. Rotation is a future concern.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt as pyjwt

# Cookie name surfaced through the OpenAPI spec.
SESSION_COOKIE_NAME = "ogma_session"

# T5.5.8: bumped from 15 minutes → 8 hours. Refresh-token machinery
# was scoped for T6+ but the 15-minute TTL combined with the lack of
# any refresh path meant customers got kicked back to SSO mid-session
# (Rick reported this on 2026-05-13). 8h is the typical workday — a
# customer who closes their laptop at lunch and reopens it still has
# a valid session. The blast-radius reasoning that motivated the
# original 15-minute window was right for a leaked-token scenario,
# but the leaked-token threat isn't worse at 8h than at 15min
# without a revocation path — and the UX cost of repeated SSO trips
# was concrete. Revisit when refresh tokens land.
SESSION_TOKEN_TTL_SECONDS = 8 * 60 * 60

# Algorithm — HS256 is fine for a single-issuer/single-audience
# setup. RS256 becomes useful when external consumers need to
# verify without sharing the secret.
JWT_ALGORITHM = "HS256"


class InvalidJwtError(Exception):
    """Raised when a token is malformed, expired, or fails verification."""


@dataclass(frozen=True)
class JwtPayload:
    """Decoded session-JWT claims, post-verification."""

    sub: str
    email: str
    sso: str
    tenant_id: Optional[str]
    iat: int
    exp: int


def _signing_key() -> str:
    """Read the signing key from env. Fails loud at first use."""
    key = os.environ.get("JWT_SIGNING_KEY", "")
    if not key:
        raise RuntimeError(
            "JWT_SIGNING_KEY is not set. Set it to a 256-bit secret "
            "(e.g., `openssl rand -hex 32`) in .env before issuing or "
            "verifying any session token."
        )
    return key


def issue_session_jwt(
    *,
    user_id: str,
    email: str,
    sso_provider: str,
    tenant_id: Optional[str] = None,
    ttl_seconds: int = SESSION_TOKEN_TTL_SECONDS,
    now: Optional[datetime] = None,
) -> str:
    """Sign and return a session JWT.

    `now` is injectable for tests that need deterministic expiry
    timestamps (the JWT round-trip test exercises both happy path
    and the expired-token branch).
    """
    if not user_id:
        raise ValueError("user_id required")
    if not email:
        raise ValueError("email required")
    if not sso_provider:
        raise ValueError("sso_provider required")

    issued_at = now or datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)

    payload = {
        "sub": user_id,
        "email": email,
        "sso": sso_provider,
        "tenant_id": tenant_id,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return pyjwt.encode(payload, _signing_key(), algorithm=JWT_ALGORITHM)


def decode_session_jwt(token: str) -> JwtPayload:
    """Verify signature + expiry, return the claims. Raises `InvalidJwtError`."""
    if not token:
        raise InvalidJwtError("empty token")

    try:
        claims = pyjwt.decode(
            token,
            _signing_key(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "email", "sso", "iat", "exp"]},
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise InvalidJwtError("session token expired") from exc
    except pyjwt.InvalidTokenError as exc:
        raise InvalidJwtError(f"session token invalid: {exc}") from exc

    return JwtPayload(
        sub=claims["sub"],
        email=claims["email"],
        sso=claims["sso"],
        tenant_id=claims.get("tenant_id"),
        iat=int(claims["iat"]),
        exp=int(claims["exp"]),
    )
