# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""SSO callback handlers — Google + Microsoft OAuth + OIDC (T4.2).

The SPA mediates the OAuth redirect: the user signs in at the
provider, the provider redirects back to the frontend with
`code` + `state`, and the frontend POSTs those to
`/api/auth/sso/{provider}/callback`. This module is what that
POST hits.

State + nonce flow:

  1. Before redirecting the user to the provider, the frontend
     generates a random `state` and a random `nonce`, then sets
     two HttpOnly cookies (`ogma_oauth_state` and
     `ogma_oauth_nonce`) and passes the same values to the
     provider in the auth URL.
  2. Provider authenticates the user and redirects back to the
     frontend's callback page with `?code=...&state=...`.
  3. Frontend POSTs `{code, state}` to this endpoint. The
     browser sends the two cookies automatically.
  4. `handle_sso_callback`:
       a. Verifies `body.state` matches the `ogma_oauth_state`
          cookie. Mismatch -> 401 (`invalid_state`) — CSRF
          defense.
       b. Calls the provider's `TokenExchanger.fetch_id_token_claims`
          (Authlib in production; substitutable stub in tests) to
          exchange the code for an ID token and parse its claims.
       c. Verifies the ID token's `nonce` claim matches the
          `ogma_oauth_nonce` cookie. Mismatch -> 401
          (`invalid_nonce`) — OIDC's defense against replayed
          authorization codes.
       d. Marks the nonce as consumed in Redis (5-minute TTL,
          single-use). Re-presenting the same nonce after this
          step fails fast with `nonce_replay`.
       e. Upserts the user row keyed on
          `(sso_provider, sso_subject_id)`.
       f. Issues the session JWT, returns it for the route to
          set in the `ogma_session` cookie.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session as ORMSession

from vargate_telemetry.auth.jwt import issue_session_jwt
from vargate_telemetry.db import SessionLocal
from vargate_telemetry.models.users import User

# Cookie names the frontend sets before redirecting; we read them
# at the callback to validate the round-trip.
STATE_COOKIE_NAME = "ogma_oauth_state"
NONCE_COOKIE_NAME = "ogma_oauth_nonce"

SUPPORTED_PROVIDERS = ("google", "microsoft")

# Nonce-replay protection window. Anthropic / Google / Microsoft
# typically rotate authorization codes inside a few minutes;
# 5 minutes is comfortably above their use windows.
NONCE_TTL_SECONDS = 5 * 60


class SsoCallbackError(Exception):
    """Caller catches and converts to an HTTP response."""

    def __init__(self, code: str, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class IdTokenClaims:
    """Subset of OIDC ID-token claims we actually use."""

    sub: str
    email: str
    name: Optional[str]
    nonce: Optional[str]


class TokenExchanger(Protocol):
    """The per-provider HTTP exchange. Production uses Authlib;
    tests substitute a stub via `set_exchanger_for_test`.

    `code` is the authorization code from the provider's redirect.
    `redirect_uri` MUST match the value used in the original auth
    request (both providers enforce this).
    """

    def fetch_id_token_claims(
        self, code: str, redirect_uri: str
    ) -> IdTokenClaims: ...


# ───────────────────────────────────────────────────────────────────────────
# Authlib-backed production exchangers.
#
# Authlib's `OAuth2Client.fetch_token` returns the access + id_token
# pair; we parse the ID token via `jwt.decode` for the claims we need.
# ───────────────────────────────────────────────────────────────────────────


def _google_exchanger() -> TokenExchanger:
    from authlib.integrations.base_client.errors import OAuthError
    from authlib.integrations.requests_client import OAuth2Session
    from authlib.jose import jwt as jose_jwt

    class _GoogleExchanger:
        TOKEN_URL = "https://oauth2.googleapis.com/token"
        JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

        def fetch_id_token_claims(
            self, code: str, redirect_uri: str
        ) -> IdTokenClaims:
            client_id = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
            client_secret = os.environ["GOOGLE_OAUTH_CLIENT_SECRET"]
            client = OAuth2Session(
                client_id=client_id, client_secret=client_secret
            )
            try:
                token = client.fetch_token(
                    self.TOKEN_URL,
                    code=code,
                    redirect_uri=redirect_uri,
                    grant_type="authorization_code",
                )
            except OAuthError as exc:
                raise SsoCallbackError(
                    "oauth_exchange_failed",
                    f"google code exchange failed: {exc}",
                ) from exc

            id_token = token.get("id_token")
            if not id_token:
                raise SsoCallbackError(
                    "oauth_exchange_failed",
                    "google response missing id_token",
                )

            # Verify against Google's published JWKS.
            import httpx

            jwks = httpx.get(self.JWKS_URL, timeout=10.0).json()
            claims = jose_jwt.decode(id_token, jwks)
            claims.validate()
            return IdTokenClaims(
                sub=str(claims["sub"]),
                email=str(claims.get("email", "")),
                name=claims.get("name"),
                nonce=claims.get("nonce"),
            )

    return _GoogleExchanger()


def _microsoft_exchanger() -> TokenExchanger:
    from authlib.integrations.base_client.errors import OAuthError
    from authlib.integrations.requests_client import OAuth2Session
    from authlib.jose import jwt as jose_jwt

    class _MicrosoftExchanger:
        TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
        JWKS_URL = "https://login.microsoftonline.com/common/discovery/v2.0/keys"

        def fetch_id_token_claims(
            self, code: str, redirect_uri: str
        ) -> IdTokenClaims:
            client_id = os.environ["MICROSOFT_OAUTH_CLIENT_ID"]
            client_secret = os.environ["MICROSOFT_OAUTH_CLIENT_SECRET"]
            client = OAuth2Session(
                client_id=client_id, client_secret=client_secret
            )
            try:
                token = client.fetch_token(
                    self.TOKEN_URL,
                    code=code,
                    redirect_uri=redirect_uri,
                    grant_type="authorization_code",
                )
            except OAuthError as exc:
                raise SsoCallbackError(
                    "oauth_exchange_failed",
                    f"microsoft code exchange failed: {exc}",
                ) from exc

            id_token = token.get("id_token")
            if not id_token:
                raise SsoCallbackError(
                    "oauth_exchange_failed",
                    "microsoft response missing id_token",
                )

            import httpx

            jwks = httpx.get(self.JWKS_URL, timeout=10.0).json()
            claims = jose_jwt.decode(id_token, jwks)
            claims.validate()
            return IdTokenClaims(
                sub=str(claims["sub"]),
                email=str(claims.get("email", claims.get("preferred_username", ""))),
                name=claims.get("name"),
                nonce=claims.get("nonce"),
            )

    return _MicrosoftExchanger()


# Test stub injection — same pattern as billing's set_dispatcher_for_test.
_test_exchangers: dict[str, TokenExchanger] = {}


def set_exchanger_for_test(
    provider: str, exchanger: Optional[TokenExchanger]
) -> None:
    """Substitute a stub TokenExchanger for the named provider. None resets."""
    if exchanger is None:
        _test_exchangers.pop(provider, None)
        return
    _test_exchangers[provider] = exchanger


def _get_exchanger(provider: str) -> TokenExchanger:
    if provider in _test_exchangers:
        return _test_exchangers[provider]
    if provider == "google":
        return _google_exchanger()
    if provider == "microsoft":
        return _microsoft_exchanger()
    raise SsoCallbackError(
        "unsupported_provider",
        f"unknown SSO provider: {provider!r}",
        status_code=400,
    )


# ───────────────────────────────────────────────────────────────────────────
# Nonce-replay tracking. Single-use, Redis-backed, 5-minute TTL.
# ───────────────────────────────────────────────────────────────────────────


_redis_client: Optional[redis.Redis] = None


def _redis_for_nonce() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(
            os.environ["REDIS_URL"], decode_responses=False
        )
    return _redis_client


def _consume_nonce_or_raise(nonce: str) -> None:
    """Mark a nonce as consumed. Replays raise SsoCallbackError."""
    key = f"vargate:oauth:nonce:{nonce}"
    r = _redis_for_nonce()
    # SET NX EX — atomic "set if not exists with TTL". Returns None if
    # the key already exists (replay), True if we just claimed it.
    claimed = r.set(key, b"used", nx=True, ex=NONCE_TTL_SECONDS)
    if not claimed:
        raise SsoCallbackError(
            "nonce_replay",
            "this OIDC nonce has already been consumed",
        )


def _reset_nonce_tracking_for_test() -> None:
    """Wipe the nonce-replay Redis namespace + client cache. Test-only."""
    global _redis_client
    if _redis_client is not None:
        for key in _redis_client.scan_iter("vargate:oauth:nonce:*"):
            _redis_client.delete(key)
    _redis_client = None


# ───────────────────────────────────────────────────────────────────────────
# User upsert
# ───────────────────────────────────────────────────────────────────────────


def _upsert_user(
    session: ORMSession,
    *,
    sso_provider: str,
    sso_subject_id: str,
    email: str,
    name: Optional[str],
) -> User:
    """Return the User row for this (provider, subject_id), creating if new.

    Also bumps `last_login_at` on every callback so we have visibility
    into sign-in activity without a separate audit table.
    """
    existing = session.execute(
        select(User).where(
            User.sso_provider == sso_provider,
            User.sso_subject_id == sso_subject_id,
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if existing is None:
        user = User(
            email=email,
            sso_provider=sso_provider,
            sso_subject_id=sso_subject_id,
            name=name,
            last_login_at=now,
            # T4.7: time-to-first-pull histogram observes
            # `now() - sso_sign_in_at` when the first telemetry row
            # lands. Setting this here means the metric reflects the
            # most-recent sign-in's wall-clock — a user who bounces
            # halfway through onboarding and re-enters gets a fresh
            # clock for the second attempt, which matches their lived
            # "started now" intent.
            sso_sign_in_at=now,
        )
        session.add(user)
        session.flush()
        return user

    existing.last_login_at = now
    existing.sso_sign_in_at = now  # T4.7 — see above.
    # Refresh email + name in case the provider's profile changed.
    if email and email != existing.email:
        existing.email = email
    if name and name != existing.name:
        existing.name = name
    session.flush()
    return existing


# ───────────────────────────────────────────────────────────────────────────
# Public entry point — the FastAPI route calls this.
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SsoCallbackResult:
    """What the route layer needs to set the session cookie + return JSON."""

    user_id: str
    email: str
    name: Optional[str]
    session_jwt: str


def handle_sso_callback(
    provider: str,
    *,
    code: str,
    body_state: str,
    state_cookie: Optional[str],
    nonce_cookie: Optional[str],
    redirect_uri: str,
) -> SsoCallbackResult:
    """Drive the full callback flow. Raises `SsoCallbackError` on any check
    that fails so the route layer can turn it into the right HTTP response.
    """
    if provider not in SUPPORTED_PROVIDERS:
        raise SsoCallbackError(
            "unsupported_provider",
            f"unknown SSO provider: {provider!r}",
            status_code=400,
        )

    # 1. CSRF: the state in the POST body MUST match the state we set
    # in the cookie before redirecting to the provider.
    if not state_cookie or state_cookie != body_state:
        raise SsoCallbackError(
            "invalid_state",
            "state cookie missing or does not match request body — "
            "likely CSRF attempt or stale tab",
        )

    # 2. Code -> ID-token claims (Authlib in production, stub in tests).
    exchanger = _get_exchanger(provider)
    claims = exchanger.fetch_id_token_claims(code, redirect_uri)

    # 3. Nonce: the ID token MUST carry the nonce we set on the
    # original auth request (defense against replayed `code`).
    if not claims.nonce or not nonce_cookie or claims.nonce != nonce_cookie:
        raise SsoCallbackError(
            "invalid_nonce",
            "id_token nonce missing or does not match the nonce cookie",
        )

    # 4. Single-use: even if state + nonce match, refuse a second
    # callback with the same nonce. Belt-and-braces against a
    # cookie-leak or attacker-controlled state cookie.
    _consume_nonce_or_raise(claims.nonce)

    # 5. Upsert the user, bump last_login_at.
    if not claims.email:
        raise SsoCallbackError(
            "missing_email",
            "id_token did not include an email claim — refusing to "
            "provision a user without a verified email",
        )
    db = SessionLocal()
    try:
        user = _upsert_user(
            db,
            sso_provider=provider,
            sso_subject_id=claims.sub,
            email=claims.email,
            name=claims.name,
        )
        db.commit()
        # Read fields out before the session closes.
        user_id = str(user.id)
        email = user.email
        name = user.name
    finally:
        db.close()

    # 6. Issue the session JWT for the route to set in `ogma_session`.
    token = issue_session_jwt(
        user_id=user_id,
        email=email,
        sso_provider=provider,
        tenant_id=None,  # T4.5 binds the tenant_id
    )
    return SsoCallbackResult(
        user_id=user_id,
        email=email,
        name=name,
        session_jwt=token,
    )
