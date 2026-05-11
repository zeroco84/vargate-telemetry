# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""FastAPI routes for SSO callbacks + `/me` (T4.2).

Paths match the `vargate-telemetry/openapi/ogma-api.yaml` contract.
`root_path="/api"` on the app prepends `/api/` in production —
this module's routes are mounted without that prefix.

Three operations:

  - `POST /auth/sso/google/callback`     — operationId `ssoGoogleCallback`
  - `POST /auth/sso/microsoft/callback`  — operationId `ssoMicrosoftCallback`
  - `GET  /me`                            — operationId `getMe`

Each callback runs `handle_sso_callback`, sets the `ogma_session`
cookie on success, and returns the user-identity JSON body. The
`/me` endpoint returns the same shape minus the cookie set.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, Field

from vargate_telemetry.auth.middleware import AuthenticatedUser, current_user
from vargate_telemetry.auth.sso import (
    NONCE_COOKIE_NAME,
    STATE_COOKIE_NAME,
    SsoCallbackError,
    handle_sso_callback,
)
from vargate_telemetry.auth.jwt import (
    SESSION_COOKIE_NAME,
    SESSION_TOKEN_TTL_SECONDS,
)
from vargate_telemetry.metrics import track_step


router = APIRouter()


# ───────────────────────────────────────────────────────────────────────────
# Pydantic shapes — match openapi/ogma-api.yaml's schemas:
#   SsoCallbackRequest, SsoCallbackResponse, ErrorResponse, Me, Tenant
# ───────────────────────────────────────────────────────────────────────────


class SsoCallbackRequest(BaseModel):
    code: str = Field(..., description="Authorization code from the provider.")
    state: str = Field(..., description="CSRF token mirrored from the auth request.")


class SsoCallbackResponse(BaseModel):
    user_id: str
    email: EmailStr
    name: Optional[str] = None


class TenantSummary(BaseModel):
    tenant_id: str
    region: str


class MeResponse(BaseModel):
    user_id: str
    email: EmailStr
    name: Optional[str] = None
    sso_provider: str
    tenants: list[TenantSummary] = Field(default_factory=list)


def _redirect_uri(provider: str) -> str:
    """Build the callback URL the provider was told to redirect to.

    Defaults to `https://vargate.ai/auth/callback/{provider}` —
    the frontend route that receives the provider's GET redirect
    and POSTs the code+state to this backend. Override per env
    with `OGMA_OAUTH_REDIRECT_BASE` (no trailing slash) when
    running locally against `localhost:5173`.
    """
    base = os.environ.get("OGMA_OAUTH_REDIRECT_BASE", "https://vargate.ai")
    return f"{base.rstrip('/')}/auth/callback/{provider}"


def _set_session_cookie(response: Response, jwt_token: str) -> None:
    """Set the session JWT in an HttpOnly cookie matching the YAML's contract."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=jwt_token,
        max_age=SESSION_TOKEN_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _clear_oauth_cookies(response: Response) -> None:
    """Wipe the short-lived state + nonce cookies after a successful callback."""
    response.delete_cookie(STATE_COOKIE_NAME, path="/")
    response.delete_cookie(NONCE_COOKIE_NAME, path="/")


def _to_http_error(exc: SsoCallbackError) -> HTTPException:
    """Map the auth-layer error to the OpenAPI ErrorResponse shape."""
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )


# ───────────────────────────────────────────────────────────────────────────
# Routes
# ───────────────────────────────────────────────────────────────────────────


@router.post(
    "/auth/sso/google/callback",
    response_model=SsoCallbackResponse,
    operation_id="ssoGoogleCallback",
    tags=["auth"],
    summary="Google OAuth 2.0 callback",
)
def sso_google_callback(
    body: SsoCallbackRequest,
    response: Response,
    ogma_oauth_state: Optional[str] = Cookie(default=None),
    ogma_oauth_nonce: Optional[str] = Cookie(default=None),
) -> SsoCallbackResponse:
    # T4.7: track_step observes on success only — a SsoCallbackError
    # path raises through the context manager and bypasses the
    # histogram observation.
    with track_step("sso"):
        try:
            result = handle_sso_callback(
                "google",
                code=body.code,
                body_state=body.state,
                state_cookie=ogma_oauth_state,
                nonce_cookie=ogma_oauth_nonce,
                redirect_uri=_redirect_uri("google"),
            )
        except SsoCallbackError as exc:
            raise _to_http_error(exc) from exc

        _set_session_cookie(response, result.session_jwt)
        _clear_oauth_cookies(response)
        return SsoCallbackResponse(
            user_id=result.user_id,
            email=result.email,
            name=result.name,
        )


@router.post(
    "/auth/sso/microsoft/callback",
    response_model=SsoCallbackResponse,
    operation_id="ssoMicrosoftCallback",
    tags=["auth"],
    summary="Microsoft OAuth 2.0 callback",
)
def sso_microsoft_callback(
    body: SsoCallbackRequest,
    response: Response,
    ogma_oauth_state: Optional[str] = Cookie(default=None),
    ogma_oauth_nonce: Optional[str] = Cookie(default=None),
) -> SsoCallbackResponse:
    with track_step("sso"):  # T4.7
        try:
            result = handle_sso_callback(
                "microsoft",
                code=body.code,
                body_state=body.state,
                state_cookie=ogma_oauth_state,
                nonce_cookie=ogma_oauth_nonce,
                redirect_uri=_redirect_uri("microsoft"),
            )
        except SsoCallbackError as exc:
            raise _to_http_error(exc) from exc

        _set_session_cookie(response, result.session_jwt)
        _clear_oauth_cookies(response)
        return SsoCallbackResponse(
            user_id=result.user_id,
            email=result.email,
            name=result.name,
        )


@router.get(
    "/me",
    response_model=MeResponse,
    operation_id="getMe",
    tags=["me"],
    summary="Return the signed-in user's profile + tenant binding",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Missing or invalid session.",
        },
    },
)
def get_me(user: AuthenticatedUser = Depends(current_user)) -> MeResponse:
    tenants: list[TenantSummary] = []
    if user.tenant_id:
        # T4.5 will surface tenant region + multi-tenant binding. For
        # T4.2 the JWT carries a single tenant_id (always null today).
        tenants.append(TenantSummary(tenant_id=user.tenant_id, region="us"))
    return MeResponse(
        user_id=user.user_id,
        email=user.email,
        sso_provider=user.sso_provider,
        tenants=tenants,
    )
