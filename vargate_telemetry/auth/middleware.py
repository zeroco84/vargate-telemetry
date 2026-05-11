# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""FastAPI dependency for SSO-backed authentication (T4.2).

`current_user` is the single source of truth for "who is making this
request." Every protected route depends on it:

    @router.get("/me")
    def me(user: AuthenticatedUser = Depends(current_user)):
        ...

It reads the JWT from either the `ogma_session` cookie (browser
flow) or the `Authorization: Bearer <jwt>` header (programmatic
flow), decodes + verifies, and returns the claim payload. Any
failure raises HTTP 401 with the standard ErrorResponse shape from
`openapi/ogma-api.yaml`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, status

from vargate_telemetry.auth.jwt import (
    SESSION_COOKIE_NAME,
    InvalidJwtError,
    JwtPayload,
    decode_session_jwt,
)


@dataclass(frozen=True)
class AuthenticatedUser:
    """The identity attached to an in-flight request after auth succeeds."""

    user_id: str
    email: str
    sso_provider: str
    tenant_id: Optional[str]

    @classmethod
    def from_jwt(cls, payload: JwtPayload) -> "AuthenticatedUser":
        return cls(
            user_id=payload.sub,
            email=payload.email,
            sso_provider=payload.sso,
            tenant_id=payload.tenant_id,
        )


def _extract_token(request: Request) -> Optional[str]:
    """Return the JWT from the request, preferring the cookie."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        return cookie

    auth_header = request.headers.get("authorization") or ""
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() == "bearer" and token:
        return token
    return None


def current_user(request: Request) -> AuthenticatedUser:
    """FastAPI dependency: 401 if no valid session, AuthenticatedUser otherwise."""
    token = _extract_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "unauthenticated",
                "message": (
                    "no session cookie and no Authorization header — "
                    "sign in at /api/auth/sso/{provider}/callback first"
                ),
            },
        )

    try:
        payload = decode_session_jwt(token)
    except InvalidJwtError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "unauthenticated",
                "message": str(exc),
            },
        ) from exc

    return AuthenticatedUser.from_jwt(payload)
