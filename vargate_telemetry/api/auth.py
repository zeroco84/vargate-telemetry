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


# ───────────────────────────────────────────────────────────────────────────
# TM8 Phase B — nested per-vendor capability shape.
#
# `/me/capabilities` grows from a flat 5-bool object into a per-vendor
# map `{anthropic:{…}, openai:{…}}`. To avoid blanking the SPA's tiles
# mid-deploy, the endpoint ALSO dual-emits the legacy flat Anthropic
# keys at the top level for one release; the flat keys are dropped once
# the frontend ships the nested consumer.
#
# `extra="allow"` is NOT used here — the response is a closed contract
# and the dual-emit flat keys are declared explicitly on the envelope
# so the OpenAPI schema documents both shapes during the rollout.
# ───────────────────────────────────────────────────────────────────────────


class AnthropicCapabilities(BaseModel):
    admin_api: bool
    activity_feed: bool
    content_capture: bool
    code_analytics: bool
    mcp_connector: bool


class OpenAICapabilities(BaseModel):
    # `admin` = the org Admin key works (usage rows have landed OR a key
    # is sealed). `costs` / `audit_logs` reflect recent rows of the
    # respective source_api. NOTE accessible ≠ populated: an org can hold
    # a working key whose audit_logs endpoint returns empty (non-
    # Enterprise) — `audit_logs` stays False until a row actually lands.
    admin: bool
    costs: bool
    audit_logs: bool
    # `project_users` = the openai_users side table has rows for the
    # tenant (projects/keys/users sync ran).
    project_users: bool
    # `per_user_breakdown` = a recent openai_admin_usage row carries a
    # non-null subject_user_id, i.e. group_by=user_id populated on this
    # org's tier (PAYG+ per the recon). Drives the honest empty-state.
    per_user_breakdown: bool


class GoogleCapabilities(BaseModel):
    # TM9: Google Vertex AI — Ogma's third vendor. Two MVP streams
    # (cost via BigQuery billing export, usage via Cloud Monitoring
    # token_count); audit is DEFERRED.
    #
    # `cost` = a recent `vertex_billing_costs` row has landed.
    # `usage` = a recent `vertex_token_usage` row has landed.
    cost: bool
    usage: bool
    # `project_attribution` is STRUCTURAL — Google always attributes
    # usage/spend to a project, so this is ALWAYS True for the Google
    # block (independent of row presence).
    project_attribution: bool
    # `team_labels` = a recent `vertex_billing_costs` row carries a
    # non-empty request-label set (the team dim). Lights only once a
    # labelled billing row has actually landed.
    team_labels: bool
    # `per_user_breakdown` is STRUCTURAL and ALWAYS False — Google has
    # NO per-user-email attribution (no Vertex users side-table, the
    # email reconciler never reads these streams). The dashboard uses
    # this to render the honest "project/team only" empty-state.
    per_user_breakdown: bool
    # `audit` is ALWAYS False — the Vertex audit stream is DEFERRED in
    # the TM9 MVP (no `vertex_*_audit` source_api exists yet).
    audit: bool


class CapabilitiesResponse(BaseModel):
    """Nested per-vendor capability snapshot + dual-emitted legacy flat keys.

    The `anthropic` / `openai` / `google` sub-objects are the forward
    shape. The five top-level bools (`admin_api`, `activity_feed`,
    `content_capture`, `code_analytics`, `mcp_connector`) are the legacy
    flat Anthropic keys, retained for one release so the current SPA keeps
    working through the deploy. They MIRROR `anthropic.*` exactly and are
    dropped once the nested consumer ships.
    """

    anthropic: AnthropicCapabilities
    openai: OpenAICapabilities
    # TM9: Google Vertex AI block (third vendor). Additive — does not
    # affect the Anthropic / OpenAI shapes.
    google: GoogleCapabilities
    # Legacy flat Anthropic keys (deprecated; mirror `anthropic.*`).
    admin_api: bool
    activity_feed: bool
    content_capture: bool
    code_analytics: bool
    mcp_connector: bool


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
    response_model=CapabilitiesResponse,
    operation_id="getMeCapabilities",
    tags=["me"],
    summary="Nested per-vendor capability snapshot (dual-emits legacy flat keys)",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Missing or invalid session.",
        },
    },
)
def get_me_capabilities(
    user: AuthenticatedUser = Depends(current_user),
) -> CapabilitiesResponse:
    """Return the per-vendor capability snapshot for the tenant.

    **Shape (TM8 Phase B, extended TM9):** nested per-vendor map —
    ``{anthropic:{…}, openai:{…}, google:{…}}`` — PLUS the legacy flat
    Anthropic keys dual-emitted at the top level for one release so the
    current SPA keeps working through the deploy. The flat keys mirror
    ``anthropic.*`` exactly and are dropped once the nested consumer
    ships.

    **Anthropic** — four bools (``admin_api``, ``activity_feed``,
    ``code_analytics``, ``mcp_connector``) answer "does the tenant have
    at least one ``telemetry_records`` row with the matching
    ``source_api`` in the last 90 days?" — same uniform semantics as the
    ``mcp_connector`` detector in onboarding.py. ``content_capture``
    (TM5 T5.1) is the exception: it answers "is a Compliance Access Key
    sealed for this tenant?" (a row in ``encrypted_secrets``), because
    the capability is unlocked by *holding the key*, not by content
    having been pulled yet.

    **OpenAI** (TM8) —
      - ``admin``: a recent ``openai_admin_usage`` row OR an
        ``openai_admin_key`` sealed in ``encrypted_secrets`` (so the
        tile lights the moment the key is sealed, before the first pull).
      - ``costs``: a recent ``openai_admin_costs`` row.
      - ``audit_logs``: a recent ``openai_audit_logs`` row. NOTE
        accessible ≠ populated — the endpoint 200s but is empty on
        non-Enterprise orgs, so this stays False until a row lands.
      - ``project_users``: the ``openai_users`` side table has rows for
        the tenant (the projects/keys/users sync ran).
      - ``per_user_breakdown``: a recent ``openai_admin_usage`` row
        carries a non-null ``subject_user_id`` (``group_by=user_id``
        populated on this org's tier) — drives the honest empty-state.

    **Google / Vertex** (TM9) — project/team attribution only, audit
    deferred:
      - ``cost``: a recent ``vertex_billing_costs`` row (BigQuery billing
        export).
      - ``usage``: a recent ``vertex_token_usage`` row (Cloud Monitoring
        token_count).
      - ``project_attribution``: STRUCTURAL — always True (Google always
        attributes to a project).
      - ``team_labels``: a recent ``vertex_billing_costs`` row carries a
        non-empty request-label set (the team dim) — lights only once a
        labelled billing row lands.
      - ``per_user_breakdown``: STRUCTURAL — always False (Google has NO
        per-user-email attribution; no users side-table, the reconciler
        never reads these streams).
      - ``audit``: always False — the Vertex audit stream is DEFERRED in
        the TM9 MVP.

    Pre-tenant users (no tenant_id) get all False (except the structural
    ``google.project_attribution``, which is always True).
    """
    if not user.tenant_id:
        anthropic_caps = AnthropicCapabilities(
            admin_api=False,
            activity_feed=False,
            content_capture=False,
            code_analytics=False,
            mcp_connector=False,
        )
        openai_caps = OpenAICapabilities(
            admin=False,
            costs=False,
            audit_logs=False,
            project_users=False,
            per_user_breakdown=False,
        )
        # Google: all-False EXCEPT the structural project_attribution
        # (always True). per_user_breakdown / audit are structurally /
        # deferred-False; cost / usage / team_labels are False with no
        # tenant to probe.
        google_caps = GoogleCapabilities(
            cost=False,
            usage=False,
            project_attribution=True,
            team_labels=False,
            per_user_breakdown=False,
            audit=False,
        )
        return CapabilitiesResponse(
            anthropic=anthropic_caps,
            openai=openai_caps,
            google=google_caps,
            admin_api=False,
            activity_feed=False,
            content_capture=False,
            code_analytics=False,
            mcp_connector=False,
        )

    from vargate_telemetry.anthropic.factory import (
        ANTHROPIC_COMPLIANCE_KEY_SECRET,
    )
    from vargate_telemetry.openai.factory import OPENAI_ADMIN_KEY_SECRET
    from vargate_telemetry.tasks.pull_openai_audit import (
        SOURCE_API_OPENAI_AUDIT,
    )
    from vargate_telemetry.tasks.pull_openai_costs import (
        SOURCE_API_OPENAI_COSTS,
    )
    from vargate_telemetry.tasks.pull_openai_usage import (
        SOURCE_API_OPENAI_USAGE,
    )
    from vargate_telemetry.tasks.pull_vertex_costs import (
        SOURCE_API_VERTEX_COSTS,
    )
    from vargate_telemetry.tasks.pull_vertex_usage import (
        SOURCE_API_VERTEX_USAGE,
    )
    from vargate_telemetry.db import session_scope

    with session_scope(user.tenant_id) as s:
        # One round-trip. Anthropic: four cheap recent-row probes
        # against ix_telemetry_records_tenant_occurred + the
        # content_capture key probe (sealed-key, not pulled-data). OpenAI
        # (TM8): three recent-row probes (usage/costs/audit), one
        # sealed-key probe (openai_admin_key — so `admin` lights on key-
        # seal before the first pull), one non-null-subject probe for
        # per_user_breakdown, and a side-table EXISTS for project_users.
        # Google (TM9): two recent-row probes (vertex_billing_costs →
        # cost, vertex_token_usage → usage) plus a labelled-cost-row probe
        # (a vertex_billing_costs row whose metadata->'labels' is a
        # non-empty object → team_labels). project_attribution /
        # per_user_breakdown / audit are STRUCTURAL constants, not probed.
        #
        # The OpenAI + Vertex source_api strings come from the pull-task
        # module constants (single source of truth), bound as params
        # rather than inlined so a rename can't silently diverge.
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
                  ) AS mcp_connector,
                  EXISTS (
                    SELECT 1 FROM encrypted_secrets
                    WHERE tenant_id = :t
                      AND secret_name = :compliance_secret
                  ) AS content_capture,
                  EXISTS (
                    SELECT 1 FROM telemetry_records
                    WHERE tenant_id = :t
                      AND source_api = :openai_usage
                      AND ingested_at > now() - INTERVAL '90 days'
                  ) AS openai_usage_rows,
                  EXISTS (
                    SELECT 1 FROM encrypted_secrets
                    WHERE tenant_id = :t
                      AND secret_name = :openai_secret
                  ) AS openai_key_sealed,
                  EXISTS (
                    SELECT 1 FROM telemetry_records
                    WHERE tenant_id = :t
                      AND source_api = :openai_costs
                      AND ingested_at > now() - INTERVAL '90 days'
                  ) AS openai_costs,
                  EXISTS (
                    SELECT 1 FROM telemetry_records
                    WHERE tenant_id = :t
                      AND source_api = :openai_audit
                      AND ingested_at > now() - INTERVAL '90 days'
                  ) AS openai_audit_logs,
                  EXISTS (
                    SELECT 1 FROM openai_users
                    WHERE tenant_id = :t
                  ) AS openai_project_users,
                  EXISTS (
                    SELECT 1 FROM telemetry_records
                    WHERE tenant_id = :t
                      AND source_api = :openai_usage
                      AND subject_user_id IS NOT NULL
                      AND ingested_at > now() - INTERVAL '90 days'
                  ) AS openai_per_user_breakdown,
                  EXISTS (
                    SELECT 1 FROM telemetry_records
                    WHERE tenant_id = :t
                      AND source_api = :vertex_costs
                      AND ingested_at > now() - INTERVAL '90 days'
                  ) AS vertex_costs,
                  EXISTS (
                    SELECT 1 FROM telemetry_records
                    WHERE tenant_id = :t
                      AND source_api = :vertex_usage
                      AND ingested_at > now() - INTERVAL '90 days'
                  ) AS vertex_usage,
                  EXISTS (
                    SELECT 1 FROM telemetry_records
                    WHERE tenant_id = :t
                      AND source_api = :vertex_costs
                      AND ingested_at > now() - INTERVAL '90 days'
                      AND metadata->'labels' IS NOT NULL
                      AND metadata->'labels' <> 'null'::jsonb
                      AND metadata->'labels' <> '{}'::jsonb
                  ) AS vertex_team_labels
                """
            ),
            {
                "t": user.tenant_id,
                "compliance_secret": ANTHROPIC_COMPLIANCE_KEY_SECRET,
                "openai_secret": OPENAI_ADMIN_KEY_SECRET,
                "openai_usage": SOURCE_API_OPENAI_USAGE,
                "openai_costs": SOURCE_API_OPENAI_COSTS,
                "openai_audit": SOURCE_API_OPENAI_AUDIT,
                "vertex_costs": SOURCE_API_VERTEX_COSTS,
                "vertex_usage": SOURCE_API_VERTEX_USAGE,
            },
        ).one()

    anthropic_caps = AnthropicCapabilities(
        admin_api=bool(row.admin_api),
        activity_feed=bool(row.activity_feed),
        # TM5 T5.1: True once a Compliance Access Key is sealed for this
        # tenant (via POST /onboarding/compliance-key) — the capability,
        # independent of whether any content has been pulled yet.
        content_capture=bool(row.content_capture),
        code_analytics=bool(row.code_analytics),
        mcp_connector=bool(row.mcp_connector),
    )
    openai_caps = OpenAICapabilities(
        # `admin` lights on EITHER a recent usage row OR a sealed key, so
        # the onboarding tile flips the moment the key is sealed (before
        # the first 15-minute pull lands a row).
        admin=bool(row.openai_usage_rows) or bool(row.openai_key_sealed),
        costs=bool(row.openai_costs),
        audit_logs=bool(row.openai_audit_logs),
        project_users=bool(row.openai_project_users),
        per_user_breakdown=bool(row.openai_per_user_breakdown),
    )
    google_caps = GoogleCapabilities(
        # `cost` / `usage` are row-presence (a recent vertex_billing_costs
        # / vertex_token_usage row landed). `team_labels` lights once a
        # labelled billing row has landed.
        cost=bool(row.vertex_costs),
        usage=bool(row.vertex_usage),
        # STRUCTURAL — Google always attributes to a project.
        project_attribution=True,
        team_labels=bool(row.vertex_team_labels),
        # STRUCTURAL — Google has NO per-user-email attribution.
        per_user_breakdown=False,
        # DEFERRED — no Vertex audit stream in the TM9 MVP.
        audit=False,
    )
    return CapabilitiesResponse(
        anthropic=anthropic_caps,
        openai=openai_caps,
        google=google_caps,
        # Dual-emit the legacy flat Anthropic keys (mirror anthropic.*)
        # for one release so the current SPA keeps working mid-deploy.
        admin_api=anthropic_caps.admin_api,
        activity_feed=anthropic_caps.activity_feed,
        content_capture=anthropic_caps.content_capture,
        code_analytics=anthropic_caps.code_analytics,
        mcp_connector=anthropic_caps.mcp_connector,
    )


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
