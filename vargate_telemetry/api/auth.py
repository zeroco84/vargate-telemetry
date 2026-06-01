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
from sqlalchemy import text as sql_text

from vargate_telemetry.auth.middleware import AuthenticatedUser, current_user
from vargate_telemetry.auth.sso import (
    NONCE_COOKIE_NAME,
    STATE_COOKIE_NAME,
    SsoCallbackError,
    handle_sso_callback,
)
from vargate_telemetry.db import scheduler_session_scope, session_scope
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
    # TM4: the caller's role in their bound tenant ('admin' | 'member'),
    # or None when not yet bound. The dashboard uses this to show/hide
    # admin-only controls (it's advisory UX only — the backend enforces).
    role: Optional[str] = None


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
    role: Optional[str] = None
    if user.tenant_id:
        # T5.5.8: look the real region up from the tenants table.
        # Pre-T5.5.8 this was hardcoded "us" — a T4.2 placeholder
        # that surfaced as a wrong region chip + wrong env label on
        # the dashboard topbar for every EU customer. Use the
        # scheduler scope (no app.tenant_id binding required) since
        # we're reading a single row for the bound user; the RLS
        # policy on `tenants` doesn't gate by app.tenant_id anyway.
        region: str = "us"  # safe fallback if the tenant row is missing
        with scheduler_session_scope() as s:
            row = s.execute(
                sql_text(
                    "SELECT region FROM tenants WHERE tenant_id = :t"
                ),
                {"t": user.tenant_id},
            ).first()
            if row is not None and row.region in ("us", "eu"):
                region = row.region
        # TM4 role gate: looked up under vargate_app (session_scope) —
        # the scheduler role has no SELECT on `users`. users has no RLS,
        # so scope by tenant_id explicitly.
        with session_scope(user.tenant_id) as s:
            role_row = s.execute(
                sql_text(
                    "SELECT role FROM users "
                    "WHERE id::text = :uid AND tenant_id = :t"
                ),
                {"uid": user.user_id, "t": user.tenant_id},
            ).first()
            if role_row is not None:
                role = role_row.role
        tenants.append(
            TenantSummary(tenant_id=user.tenant_id, region=region)
        )
    return MeResponse(
        user_id=user.user_id,
        email=user.email,
        sso_provider=user.sso_provider,
        tenants=tenants,
        role=role,
    )


# ───────────────────────────────────────────────────────────────────────────
# TM2 Phase D2 — GET /me/capabilities
# ───────────────────────────────────────────────────────────────────────────
#
# The dashboard fetches this on mount to reconcile its sessionStorage
# capability snapshot with the current tenant-state. Per the TM2
# CLAUDE.md rule on capability surfacing, the value reflects ACTUAL
# usage in the last 90 days, not "was this key probed at onboarding."
# A tenant that's been onboarded but never had data ingest still
# reads False everywhere — the SPA tiles only light up after rows
# actually arrive.


@router.get(
    "/me/capabilities",
    operation_id="getMeCapabilities",
    tags=["me"],
    summary="Active-data-based 5-bool capability snapshot for the tenant",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Missing or invalid session.",
        },
    },
)
def get_me_capabilities(
    user: AuthenticatedUser = Depends(current_user),
) -> dict:
    """Return the 5-bool capability shape from telemetry_records state.

    Each bool answers: "does the tenant have at least one row with
    the matching ``source_api`` in the last 90 days?" Same uniform
    semantics as the ``mcp_connector`` detector in onboarding.py.

    Pre-tenant users (no tenant_id) get all False.
    """
    if not user.tenant_id:
        return {
            "admin_api": False,
            "activity_feed": False,
            "content_capture": False,
            "code_analytics": False,
            "mcp_connector": False,
        }

    from vargate_telemetry.db import session_scope

    with session_scope(user.tenant_id) as s:
        # One round-trip — five EXISTS subqueries. Each one is
        # cheap (single-row probe against
        # ix_telemetry_records_tenant_occurred). content_capture
        # stays a hard False per the T5.3 invariant.
        row = s.execute(
            sql_text(
                """
                SELECT
                  EXISTS (
                    SELECT 1 FROM telemetry_records
                    WHERE tenant_id = :t
                      AND source_api = 'admin'
                      AND ingested_at > now() - INTERVAL '90 days'
                  ) AS admin_api,
                  EXISTS (
                    SELECT 1 FROM telemetry_records
                    WHERE tenant_id = :t
                      AND source_api = 'compliance_activities'
                      AND ingested_at > now() - INTERVAL '90 days'
                  ) AS activity_feed,
                  EXISTS (
                    SELECT 1 FROM telemetry_records
                    WHERE tenant_id = :t
                      AND source_api = 'code_analytics'
                      AND ingested_at > now() - INTERVAL '90 days'
                  ) AS code_analytics,
                  EXISTS (
                    SELECT 1 FROM telemetry_records
                    WHERE tenant_id = :t
                      AND source_api = 'mcp'
                      AND ingested_at > now() - INTERVAL '90 days'
                  ) AS mcp_connector
                """
            ),
            {"t": user.tenant_id},
        ).one()

    return {
        "admin_api": bool(row.admin_api),
        "activity_feed": bool(row.activity_feed),
        # T5.3 invariant: content_capture is always False until the
        # Compliance Access Key onboarding step lands.
        "content_capture": False,
        "code_analytics": bool(row.code_analytics),
        "mcp_connector": bool(row.mcp_connector),
    }


# ───────────────────────────────────────────────────────────────────────────
# POST /auth/logout — clear the session cookie
# ───────────────────────────────────────────────────────────────────────────


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="logout",
    tags=["auth"],
    summary="Clear the session cookie and sign the user out",
    responses={
        status.HTTP_204_NO_CONTENT: {
            "description": "Session cookie cleared. The response carries "
            "a `Set-Cookie` that overwrites `ogma_session` with an "
            "empty value and `Max-Age=0` so the browser drops it.",
        },
    },
)
def logout(response: Response) -> Response:
    """Clear the session cookie.

    Intentionally NOT gated on ``current_user``. The cookie may already
    be expired or invalid by the time the user clicks "Sign out"; we
    still want the cookie cleared. Logout is idempotent — calling it
    when no session exists is a no-op success.

    Cookie-clear shape MUST mirror ``_set_session_cookie`` exactly
    (same name, path, httponly, secure, samesite). The browser only
    overwrites cookies whose attributes match — a mismatch leaves the
    original cookie in place. The trick is ``set_cookie(...value="",
    max_age=0, ...)`` rather than ``delete_cookie`` because the latter
    omits some attributes and produces inconsistent behavior across
    browsers when the cookie was originally set with `Secure` /
    `SameSite=Lax`.
    """
    # T4.2-style cookie set: same params as `_set_session_cookie` with
    # value cleared + max_age=0 + expires=epoch. Belt-and-braces:
    # browsers vary on which attribute triggers eviction.
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value="",
        max_age=0,
        expires=0,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
