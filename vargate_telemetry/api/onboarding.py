# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Onboarding routes — key validation, region select, backfill kick-off.

T4.4 ships `POST /onboarding/validate-key`. T4.5 adds
`POST /onboarding/select-region`. T4.6 adds `start-backfill` +
`backfill-status` — the last leg of the onboarding flow.

`validate-key` is the live probe the UI uses to gate the "Continue"
button on the paste-key screen. It builds an EPHEMERAL
`AnthropicAdminClient` from the supplied key (no persistence —
the key only gets sealed in T4.5 after region confirmation),
calls `list_workspaces()` and `list_members()` against Anthropic,
and returns a structured capability report.

`select-region` is the commit point. It runs as ONE Postgres
transaction that:

  1. Generates a tenant_id (injectable for tests via
     `set_tenant_id_generator_for_test`).
  2. INSERTs the `tenants` row (under the bootstrap role — the
     T3.4 role split revokes `vargate_app` from this table).
  3. UPDATEs `users.tenant_id` to bind the SSO identity to the
     fresh tenant.
  4. Switches to `vargate_app` + sets `app.tenant_id` GUC so RLS
     applies for the per-tenant rows that follow.
  5. INSERTs the wrapped DEK into `tenant_deks` (HSM wrap is done
     in-memory before the INSERT so a wrap failure rolls back the
     tenants + users writes cleanly).
  6. AES-GCM-encrypts the admin_key with the fresh DEK and
     INSERTs into `encrypted_secrets` under the canonical
     `anthropic_admin_key` name.
  7. Reissues the session JWT with the new `tenant_id` claim and
     sets it on the response cookie.

A mid-flow failure — HSM unavailable, integrity-tag computation
breakdown, or a duplicate tenant_id from the injectable generator
— rolls the entire transaction back, leaving no orphan rows.
The idempotency path detects an already-bound user and returns
the existing tenant instead of re-provisioning.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx
from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text

from vargate_telemetry.anthropic import AnthropicAdminClient
from vargate_telemetry.anthropic.exceptions import (
    AnthropicAPIError,
    InsufficientScope,
    RateLimited,
)
from vargate_telemetry.auth.jwt import (
    SESSION_COOKIE_NAME,
    SESSION_TOKEN_TTL_SECONDS,
    issue_session_jwt,
)
from vargate_telemetry.auth.middleware import AuthenticatedUser, current_user
from vargate_telemetry.celery_app import celery_app
from vargate_telemetry.crypto.dek import encrypt_with_dek, generate_dek
from vargate_telemetry.crypto.hsm import wrap_dek
from vargate_telemetry.crypto.integrity import compute_integrity_tag
from vargate_telemetry.db import SessionLocal
from vargate_telemetry.metrics import record_completion, track_step
from vargate_telemetry.tasks.pull_admin import backfill_admin_for_tenant


router = APIRouter()


# ───────────────────────────────────────────────────────────────────────────
# Pydantic shapes — match the YAML's ValidateKeyRequest / KeyCapabilities /
# ValidateKeyResponse / SelectRegionRequest / Tenant.
# ───────────────────────────────────────────────────────────────────────────


class ValidateKeyRequest(BaseModel):
    admin_key: str = Field(
        ...,
        min_length=30,
        description="Anthropic admin API key (begins `sk-ant-admin01-`).",
    )


class KeyCapabilities(BaseModel):
    """Per-key capability report — see OpenAPI ``KeyCapabilities``.

    T5.3 split the earlier ``compliance_api: bool`` into two booleans:

      - ``activity_feed`` — set by a real probe against
        ``GET /v1/compliance/activities?limit=1``. True if 200,
        False if 403 (or any other failure).
      - ``content_capture`` — reserved field; T5.3 always returns
        ``False`` because content endpoints require a Compliance
        Access Key (a separate key type from the Admin API key
        collected by today's onboarding). A future sprint adds the
        Compliance Access Key onboarding step; until then the
        frontend renders this row as "Enable later — requires
        Compliance Access Key".

    TM1 added ``mcp_connector``: True iff this tenant has at least
    one ``telemetry_records`` row with ``source_api='mcp'`` in the
    last 90 days. Unlike the other four (which probe Anthropic),
    this signal reflects **actual runtime usage** — the capability
    only lights up once Claude has called ``log_interaction`` for
    real. If the caller has no bound tenant_id yet (i.e., they're
    still in pre-tenant onboarding), this is False.
    """

    admin_api: bool
    activity_feed: bool
    content_capture: bool
    code_analytics: bool
    mcp_connector: bool


class ValidateKeyResponse(BaseModel):
    org_name: str
    capabilities: KeyCapabilities


# Pydantic v2 has no `Literal[...]` import constraint, but we need a
# narrow region enum for the YAML's `Region: enum [us, eu]`. Using a
# str + validator keeps the body shape simple and gives clean 422s
# from the framework on bad input.

_VALID_REGIONS = ("us", "eu")


class SelectRegionRequest(BaseModel):
    region: str = Field(
        ...,
        description="Data-residency region. Once set, MUST NOT change.",
    )
    admin_key: str = Field(
        ...,
        min_length=30,
        description=(
            "Anthropic admin key, previously validated by "
            "`/onboarding/validate-key`. Sealed here in the same "
            "transaction that creates the tenant row."
        ),
    )

    def normalized_region(self) -> str:
        r = self.region.strip().lower()
        if r not in _VALID_REGIONS:
            raise ValueError(
                f"region must be one of {_VALID_REGIONS!r} (got {self.region!r})"
            )
        return r


class TenantResponse(BaseModel):
    tenant_id: str
    region: str


# ───────────────────────────────────────────────────────────────────────────
# Client-factory injection point.
#
# Tests substitute a stub via `set_client_factory_for_test`. Production
# returns the live `AnthropicAdminClient`. Same pattern as billing's
# `set_dispatcher_for_test` and SSO's `set_exchanger_for_test`.
# ───────────────────────────────────────────────────────────────────────────


ClientFactory = Callable[[str], AnthropicAdminClient]


def _default_client_factory(admin_key: str) -> AnthropicAdminClient:
    """Build the live client. Short min_wait keeps the validate-key
    response snappy even when Anthropic returns a transient 429."""
    return AnthropicAdminClient(api_key=admin_key, min_wait=1.0, max_wait=8.0)


_client_factory: ClientFactory = _default_client_factory


def set_client_factory_for_test(factory: Optional[ClientFactory]) -> None:
    """Substitute a client factory for tests. Pass `None` to reset."""
    global _client_factory
    _client_factory = factory if factory is not None else _default_client_factory


# ───────────────────────────────────────────────────────────────────────────
# Tenant-id generator injection point.
#
# The default produces a stable region-prefixed identifier; tests
# substitute a generator to force collisions (rollback test) or fix
# a known id for assertions.
# ───────────────────────────────────────────────────────────────────────────


TenantIdGenerator = Callable[[str], str]


def _default_tenant_id_generator(region: str) -> str:
    """Build a stable, region-prefixed tenant identifier.

    Format: `tnt_{region}_{16-hex-chars}`. The hex slice is enough
    entropy that two concurrent generators won't collide in practice
    while still being short enough to read in logs.
    """
    return f"tnt_{region}_{uuid.uuid4().hex[:16]}"


_tenant_id_generator: TenantIdGenerator = _default_tenant_id_generator


def set_tenant_id_generator_for_test(
    generator: Optional[TenantIdGenerator],
) -> None:
    """Substitute a tenant_id generator for tests. Pass `None` to reset."""
    global _tenant_id_generator
    _tenant_id_generator = (
        generator if generator is not None else _default_tenant_id_generator
    )


# ───────────────────────────────────────────────────────────────────────────
# /onboarding/validate-key
# ───────────────────────────────────────────────────────────────────────────


_INVALID_KEY_MESSAGE = (
    "Anthropic rejected this admin key. Re-issue it from the Anthropic "
    "console and try again. The key needs Admin API access — make sure "
    "you selected `Full access` when creating it."
)


def _tenant_has_recent_mcp_traffic(tenant_id: Optional[str]) -> bool:
    """TM1 — runtime signal for the ``mcp_connector`` capability.

    Returns True iff this tenant has at least one row in
    ``telemetry_records`` with ``source_api = 'mcp'`` ingested in
    the last 90 days. Returns False if ``tenant_id`` is None — the
    pre-select-region onboarding caller hasn't been assigned a
    tenant yet, so they can't have any traffic.

    Uses ``session_scope`` so RLS applies — even though we're only
    SELECTing a literal, the tenant-pinned context lets PG short-
    circuit the policy check.
    """
    if not tenant_id:
        return False
    # Late import — `session_scope` is the blessed entrypoint, but
    # importing it at module load forces the env-dep `DATABASE_URL`
    # to exist at import time, which would break a few unit tests
    # that monkeypatch the client factory before any DB is up.
    from vargate_telemetry.db import session_scope

    with session_scope(tenant_id) as s:
        row = s.execute(
            sql_text(
                "SELECT 1 FROM telemetry_records "
                "WHERE tenant_id = :t "
                "  AND source_api = 'mcp' "
                "  AND ingested_at > now() - INTERVAL '90 days' "
                "LIMIT 1"
            ),
            {"t": tenant_id},
        ).first()
        return row is not None


def _is_auth_failure(exc: BaseException) -> bool:
    """True iff this exception is a 401/403 from Anthropic.

    The Anthropic client surfaces three exception shapes for the
    credential-rejection family:

      - ``InsufficientScope`` (T5.2) — typed 403; AnthropicAPIError
        subclass that carries the scope hint. For validate-key probing,
        a 403 still means "this key can't probe workspaces" — user-
        facing message is the same as a 401, so they're both auth
        failures here.
      - ``httpx.HTTPStatusError`` — bare 401 (no typed wrapper yet)
        from ``raise_for_status()``.
      - Anything else AnthropicAPIError (5xx, etc.) — NOT an auth
        failure.
    """
    if isinstance(exc, InsufficientScope):
        return True
    if isinstance(exc, AnthropicAPIError):
        return False  # 5xx — not an auth failure
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (401, 403)
    return False


@router.post(
    "/onboarding/validate-key",
    response_model=ValidateKeyResponse,
    operation_id="validateAdminKey",
    tags=["onboarding"],
    summary="Validate an Anthropic admin API key",
)
def validate_key(
    body: ValidateKeyRequest,
    user: AuthenticatedUser = Depends(current_user),
) -> ValidateKeyResponse:
    """Probe Anthropic to determine which APIs the supplied key
    can reach. Returns the discovered capabilities + the org name.

    Does NOT seal the key. T4.5's `select-region` is the endpoint
    that owns the seal — by the time we get there the user has
    confirmed both the org and the region, which is the right
    boundary for crypto material.
    """
    # T4.7: observe step duration on success only. An exception path
    # (invalid key 400, rate limit 503, etc.) skips the observation.
    with track_step("validate-key"):
        client = _client_factory(body.admin_key)

        # 1. Admin API + org-name probe. If this fails with 401/403 the
        # key is unusable; surface the canonical `invalid_admin_key`
        # error so the UI can render the "this key doesn't work" state.
        org_name: Optional[str] = None
        try:
            first_workspace = next(iter(client.list_workspaces()), None)
            admin_api_ok = True
            if first_workspace is not None:
                org_name = first_workspace.name
        except RateLimited as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "anthropic_rate_limited",
                    "message": (
                        f"Anthropic rate-limited the probe; retry in "
                        f"{exc.retry_after}s."
                    ),
                },
            ) from exc
        except Exception as exc:
            if _is_auth_failure(exc):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "invalid_admin_key",
                        "message": _INVALID_KEY_MESSAGE,
                    },
                ) from exc
            raise

        # 2. Activity Feed probe (T5.3). Real signal for "this key
        # can ingest Compliance API activity events" — fetch a single
        # activity and check the status code. The previous T4.4
        # implementation probed `list_members` as a proxy, which was
        # the wrong signal: list_members works on any plan with an
        # admin key, but Activity Feed access is plan-gated
        # (Enterprise only) and scope-gated (`read:compliance_activities`).
        try:
            next(iter(client.list_activities(limit=1)), None)
            activity_feed_ok = True
        except InsufficientScope:
            # 403 — key lacks `read:compliance_activities` scope, or
            # the org's plan tier doesn't include Compliance API.
            activity_feed_ok = False
        except Exception as exc:
            if _is_auth_failure(exc):
                activity_feed_ok = False
            else:
                # A 5xx from Anthropic is not the user's fault — but
                # also doesn't confirm scope. Default to False so the
                # UI doesn't promise a capability we couldn't verify.
                activity_feed_ok = False

        # 3. Content capture: reserved field, always False in T5.3.
        # Content endpoints require a Compliance Access Key
        # (sk-ant-api01-...), which is a separate key type from the
        # Admin API key (sk-ant-admin01-...) collected by today's
        # onboarding. A future sprint adds the Compliance Access Key
        # onboarding step; until then the UI renders this as
        # "Enable later — requires Compliance Access Key".
        content_capture_ok = False

        # 4. Code Analytics probe (T5.4). Per Anthropic's docs,
        # `/v1/organizations/usage_report/claude_code` is "free to use
        # for all organizations with access to the Admin API" — so a
        # 200 here is the common case, even for orgs on Personal /
        # API-only plans. The 403 case is rare (e.g., Claude Platform
        # on AWS, which doesn't expose this endpoint at all).
        #
        # We probe yesterday's date (UTC) with limit=1 because: today's
        # data isn't complete (1-hour data-freshness lag per the docs),
        # and limit=1 keeps the probe cost minimal. The endpoint
        # responds 200 with empty `data` for any org that has Admin
        # API access but zero Claude Code usage — that's still a
        # "capability available" signal for our UI.
        from datetime import date, timedelta

        try:
            probe_day = date.today() - timedelta(days=1)
            # Eager-consume one item via `next(iter(...))` so the
            # generator's first network call fires inside the try.
            next(iter(client.list_code_analytics(starting_at=probe_day, limit=1)), None)
            code_analytics_ok = True
        except InsufficientScope:
            code_analytics_ok = False
        except Exception as exc:
            if _is_auth_failure(exc):
                code_analytics_ok = False
            else:
                # 5xx or other unexpected — default to False so we
                # don't promise a capability we couldn't verify, but
                # log loudly so we notice when this drifts.
                code_analytics_ok = False

        # 5. MCP connector probe (TM1). Unlike the four above, this
        # is NOT a probe of Anthropic — it's a runtime signal about
        # whether THIS tenant has ever received an `mcp` row in
        # telemetry_records (last 90 days). A bound tenant_id is
        # required; the pre-select-region caller gets False.
        mcp_connector_ok = _tenant_has_recent_mcp_traffic(user.tenant_id)

        return ValidateKeyResponse(
            org_name=org_name or "Your Anthropic Organization",
            capabilities=KeyCapabilities(
                admin_api=admin_api_ok,
                activity_feed=activity_feed_ok,
                content_capture=content_capture_ok,
                code_analytics=code_analytics_ok,
                mcp_connector=mcp_connector_ok,
            ),
        )


# ───────────────────────────────────────────────────────────────────────────
# /onboarding/select-region
# ───────────────────────────────────────────────────────────────────────────


_ADMIN_KEY_SECRET_NAME = "anthropic_admin_key"


def _aad_for_admin_key(tenant_id: str) -> bytes:
    """AAD binding the sealed admin key to (tenant_id, secret_name).

    Mirrors `vargate_telemetry.crypto.seal._aad_for_secret` so that
    `unseal_secret(tenant_id, "anthropic_admin_key")` decrypts the
    blob we wrote here. We inline the encrypt+INSERT here (rather
    than calling `seal_secret`) so the writes stay inside the same
    transaction as the tenant INSERT.
    """
    return f"vargate.telemetry/secret/{tenant_id}/{_ADMIN_KEY_SECRET_NAME}".encode(
        "utf-8"
    )


def _user_uuid_or_400(raw_user_id: str) -> uuid.UUID:
    """The JWT carries the user_id as a string. Cast to UUID; reject
    cleanly if the token was issued for a non-UUID identity (which
    means it's not a real user row)."""
    try:
        return uuid.UUID(raw_user_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_user",
                "message": (
                    "The session is not bound to a real user. Sign in "
                    "again and retry."
                ),
            },
        ) from exc


def _set_session_cookie(response: Response, jwt_token: str) -> None:
    """Set the session JWT in an HttpOnly cookie. Mirrors the helper
    in `api/auth.py` so the cookie shape stays identical to the SSO
    callback's; we intentionally don't share a module so each route
    file owns its cookie surface.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=jwt_token,
        max_age=SESSION_TOKEN_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


@router.post(
    "/onboarding/select-region",
    response_model=TenantResponse,
    operation_id="selectRegion",
    tags=["onboarding"],
    summary="Pick a data-residency region and provision the tenant",
)
def select_region(
    body: SelectRegionRequest,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
) -> TenantResponse:
    """Provision a tenant and seal the admin key in one transaction.

    Concurrency / failure surface:
      - HSM wrap failure → 500, no DB rows written.
      - Generator returns a tenant_id already in `tenants` →
        PK conflict → rollback, no rows leaked.
      - User already has `tenant_id` set:
          • region matches → 200 with the existing tenant row.
          • region differs → 409 `region_already_set`.
    """
    # T4.7: observe step duration on success only. The context manager
    # delegates to the underlying impl so the original 200-line body
    # doesn't need re-indenting; an HTTPException raised inside skips
    # the observation by design.
    with track_step("select-region"):
        return _select_region_impl(body, response, user)


def _select_region_impl(
    body: SelectRegionRequest,
    response: Response,
    user: AuthenticatedUser,
) -> TenantResponse:
    try:
        region = body.normalized_region()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc

    user_uuid = _user_uuid_or_400(user.user_id)

    s = SessionLocal()
    try:
        # ───── 1. Idempotency check ──────────────────────────────────────
        # SELECT under the bootstrap role — works on `users` (no RLS)
        # AND on `tenants` (vargate_app is REVOKED from it, but the
        # bootstrap role is unrestricted). Read-modify-write is safe
        # inside one transaction.
        existing_tenant_id = s.execute(
            sql_text("SELECT tenant_id FROM users WHERE id = :uid"),
            {"uid": str(user_uuid)},
        ).scalar()

        if existing_tenant_id is not None:
            existing_region = s.execute(
                sql_text(
                    "SELECT region FROM tenants WHERE tenant_id = :t"
                ),
                {"t": existing_tenant_id},
            ).scalar()
            if existing_region is None:
                # Shouldn't happen: tenant_id on users was set without
                # the matching tenants row. Treat as a 500 — the
                # invariant the endpoint maintains has been violated.
                s.rollback()
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "code": "internal_error",
                        "message": (
                            "User is bound to a tenant that no longer "
                            "exists. Contact support."
                        ),
                    },
                )
            if existing_region != region:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "region_already_set",
                        "message": (
                            f"This account is already provisioned in "
                            f"{existing_region!r}. Regions are "
                            "immutable; create a new account for a "
                            "different region."
                        ),
                    },
                )
            # Same region → idempotent replay. Reissue the JWT (in
            # case the caller lost the previous cookie) and return.
            s.commit()
            _reissue_and_set_cookie(response, user, existing_tenant_id)
            return TenantResponse(
                tenant_id=existing_tenant_id, region=existing_region
            )

        # ───── 2. Generate the tenant_id ─────────────────────────────────
        tenant_id = _tenant_id_generator(region)

        # ───── 3. Pre-compute crypto material in memory ─────────────────
        # All HSM / AES-GCM work happens BEFORE we touch the per-tenant
        # rows; if any of these raise, the transaction rolls back with
        # zero DB writes (we only got as far as the idempotency reads).
        kek_label = os.environ["HSM_KEK_LABEL"]
        dek = generate_dek()
        wrapped = wrap_dek(dek)
        dek_integrity_tag = compute_integrity_tag(tenant_id, wrapped)

        iv, ciphertext = encrypt_with_dek(
            dek,
            body.admin_key.encode("utf-8"),
            aad=_aad_for_admin_key(tenant_id),
        )
        secret_integrity_tag = compute_integrity_tag(tenant_id, ciphertext)

        # ───── 4. INSERT tenants (bootstrap role) ────────────────────────
        # `tenants` was REVOKE'd from vargate_app in 0009, so this MUST
        # run as the bootstrap role.
        now = datetime.now(timezone.utc)
        s.execute(
            sql_text(
                """
                INSERT INTO tenants
                    (tenant_id, region, active, billing_status,
                     created_at, updated_at)
                VALUES
                    (:tenant_id, :region, TRUE, 'trial',
                     :now, :now)
                """
            ),
            {"tenant_id": tenant_id, "region": region, "now": now},
        )

        # ───── 5. UPDATE users.tenant_id + role (bootstrap role) ─────────
        # The user provisioning the tenant owns it → admin (TM4 role
        # gate). Additional members default to 'member' and can be
        # promoted later by an admin.
        s.execute(
            sql_text(
                "UPDATE users SET tenant_id = :t, role = 'admin' "
                "WHERE id = :uid"
            ),
            {"t": tenant_id, "uid": str(user_uuid)},
        )

        # ───── 6. Switch to vargate_app + set tenant GUC ─────────────────
        # SET LOCAL ROLE + set_config(..., true) are both
        # transaction-scoped. RLS on `tenant_deks` and
        # `encrypted_secrets` requires both.
        s.execute(sql_text("SET LOCAL ROLE vargate_app"))
        s.execute(
            sql_text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": tenant_id},
        )

        # ───── 7. INSERT tenant_deks ─────────────────────────────────────
        s.execute(
            sql_text(
                """
                INSERT INTO tenant_deks
                    (tenant_id, wrapped_dek, kek_label, integrity_tag,
                     created_at)
                VALUES
                    (:tenant_id, :wrapped_dek, :kek_label,
                     :integrity_tag, :now)
                """
            ),
            {
                "tenant_id": tenant_id,
                "wrapped_dek": wrapped,
                "kek_label": kek_label,
                "integrity_tag": dek_integrity_tag,
                "now": now,
            },
        )

        # ───── 8. INSERT encrypted_secrets (anthropic_admin_key) ─────────
        s.execute(
            sql_text(
                """
                INSERT INTO encrypted_secrets
                    (tenant_id, secret_name, iv, ciphertext,
                     integrity_tag, created_at, last_rotated_at)
                VALUES
                    (:tenant_id, :secret_name, :iv, :ciphertext,
                     :integrity_tag, :now, :now)
                """
            ),
            {
                "tenant_id": tenant_id,
                "secret_name": _ADMIN_KEY_SECRET_NAME,
                "iv": iv,
                "ciphertext": ciphertext,
                "integrity_tag": secret_integrity_tag,
                "now": now,
            },
        )

        s.commit()
    except HTTPException:
        s.rollback()
        raise
    except Exception as exc:
        # Rolls back tenants + users + tenant_deks + encrypted_secrets
        # atomically — they're all in this transaction. Surface as a
        # 500 with our canonical error shape so the UI and TestClient
        # both see a normal HTTP response (rather than the raw DB
        # exception leaking up the stack).
        s.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "provisioning_failed",
                "message": (
                    "Tenant provisioning failed and was rolled back. "
                    "No data was written; retry the request. If the "
                    "problem persists, contact support."
                ),
            },
        ) from exc
    finally:
        s.close()

    # ───── 9. Reissue the JWT carrying the new tenant_id claim ───────────
    # Outside the transaction — the DB write is authoritative; the
    # cookie is a UX nicety. If JWT issuance fails, the tenant is
    # still provisioned and the user can sign in fresh to pick up
    # the bound claim.
    _reissue_and_set_cookie(response, user, tenant_id)

    return TenantResponse(tenant_id=tenant_id, region=region)


def _reissue_and_set_cookie(
    response: Response,
    user: AuthenticatedUser,
    tenant_id: str,
) -> None:
    """Issue a fresh session JWT carrying the new tenant_id claim and
    set it on the response cookie. Kept as a helper so both the
    happy-path and idempotent-replay branches share it."""
    new_jwt = issue_session_jwt(
        user_id=user.user_id,
        email=user.email,
        sso_provider=user.sso_provider,
        tenant_id=tenant_id,
    )
    _set_session_cookie(response, new_jwt)


# ───────────────────────────────────────────────────────────────────────────
# /onboarding/start-backfill  + /onboarding/backfill-status/{task_id}
#
# T4.6's loading-screen pair. The frontend POSTs `start-backfill`
# once on mount, then polls `backfill-status` every ~2s until the
# task hits SUCCESS or FAILURE.
#
# Both endpoints are tenant-scoped: the user can only schedule a
# backfill for THEIR OWN tenant, and they can only poll a task id
# that was recorded against their own tenant's `tenants` row. The
# task id is stored on the tenant row at enqueue time precisely so
# the poll endpoint has something to gate against — Celery's own
# AsyncResult can't tell us "this task id belongs to user X."
# ───────────────────────────────────────────────────────────────────────────


class StartBackfillRequest(BaseModel):
    tenant_id: str = Field(..., description="The tenant to backfill.")
    days: int = Field(
        90,
        ge=1,
        le=365,
        description=(
            "How many days of history to pull. Defaults to 90 (the "
            "T3.6 backfill default). Bounded to avoid runaway pulls."
        ),
    )


class StartBackfillResponse(BaseModel):
    task_id: str


class BackfillStatusResponse(BaseModel):
    state: str  # PENDING | STARTED | PROGRESS | SUCCESS | FAILURE
    chunks_processed: Optional[int] = None
    inserted: Optional[int] = None
    deduped: Optional[int] = None
    error: Optional[str] = None


# ── Injection seams ────────────────────────────────────────────────────────
#
# Production wires the live Celery task + the real AsyncResult. Tests
# substitute lightweight stubs so we can assert on dispatch args and
# control the polled state without standing up a worker.

TaskDispatcher = Callable[[str, int], Any]
"""(tenant_id, days) -> an object exposing `.id` (Celery's
`AsyncResult` does; the test stub mimics)."""


AsyncResultFactory = Callable[[str], Any]
"""(task_id) -> an object exposing `.state` and `.info` like
`celery.result.AsyncResult`."""


def _default_task_dispatcher(tenant_id: str, days: int) -> Any:
    return backfill_admin_for_tenant.delay(tenant_id, days=days)


def _default_async_result_factory(task_id: str) -> AsyncResult:
    return AsyncResult(task_id, app=celery_app)


_task_dispatcher: TaskDispatcher = _default_task_dispatcher
_async_result_factory: AsyncResultFactory = _default_async_result_factory


def set_task_dispatcher_for_test(
    dispatcher: Optional[TaskDispatcher],
) -> None:
    """Substitute the Celery `.delay` dispatcher for tests. Pass `None` to reset."""
    global _task_dispatcher
    _task_dispatcher = (
        dispatcher if dispatcher is not None else _default_task_dispatcher
    )


def set_async_result_factory_for_test(
    factory: Optional[AsyncResultFactory],
) -> None:
    """Substitute the AsyncResult factory for tests. Pass `None` to reset."""
    global _async_result_factory
    _async_result_factory = (
        factory if factory is not None else _default_async_result_factory
    )


# ── Helpers ────────────────────────────────────────────────────────────────


def _fetch_user_tenant(user: AuthenticatedUser) -> Optional[tuple[str, bool]]:
    """Return `(tenant_id, active)` for the authenticated user's tenant,
    or `None` if the user has no tenant binding yet.

    Reads `users.tenant_id` (no RLS — `users` is global) then joins
    `tenants` (bootstrap-only access; we run as the bootstrap role
    inside this short-lived session). Two round-trips would also work
    but a single CTE / SELECT keeps the latency bounded.
    """
    user_uuid = _user_uuid_or_400(user.user_id)
    s = SessionLocal()
    try:
        row = s.execute(
            sql_text(
                """
                SELECT t.tenant_id, t.active
                FROM users u
                LEFT JOIN tenants t ON t.tenant_id = u.tenant_id
                WHERE u.id = :uid
                """
            ),
            {"uid": str(user_uuid)},
        ).first()
    finally:
        s.close()
    if row is None or row.tenant_id is None:
        return None
    return (row.tenant_id, bool(row.active))


def _format_failure(info: Any) -> str:
    """Render a FAILURE task's info as a short, leak-safe string.

    Celery exposes the raised exception as `.info` (and `.result`). We
    don't want stack traces in the response — the frontend renders this
    inline. Format: `<ExceptionClass>: <message>`, truncated to 1000
    chars per the YAML contract.
    """
    if info is None:
        return "unknown_error: task failed without a reportable cause"
    if isinstance(info, BaseException):
        msg = f"{type(info).__name__}: {info}"
    else:
        msg = f"{type(info).__name__}: {info!s}"
    return msg[:1000]


# ── POST /onboarding/start-backfill ────────────────────────────────────────


@router.post(
    "/onboarding/start-backfill",
    response_model=StartBackfillResponse,
    operation_id="startBackfill",
    tags=["onboarding"],
    summary="Enqueue the historical backfill for the user's tenant",
)
def start_backfill(
    body: StartBackfillRequest,
    user: AuthenticatedUser = Depends(current_user),
) -> StartBackfillResponse:
    """Dispatch `backfill_admin_for_tenant` (T3.6) and record the task
    id on the tenant row so the matching status endpoint can scope
    polling.

    Idempotency: if the tenant already has `initial_backfill_task_id`
    set, the existing id is returned unchanged. This is what the
    frontend's "refresh the page mid-backfill" path relies on — the
    second page load re-POSTs and gets the same task to poll.
    """
    # T4.7: start-backfill is the last server-side gate of onboarding,
    # so we observe both the step duration AND the `completed` outcome
    # counter on success. The wrapping context manager guarantees the
    # error paths (403, 404, 400, 500) bypass observation by design.
    with track_step("start-backfill"):
        result = _start_backfill_impl(body, user)
        record_completion("completed")
        return result


def _start_backfill_impl(
    body: StartBackfillRequest,
    user: AuthenticatedUser,
) -> StartBackfillResponse:
    # 1. Cross-tenant guard. We compare the body's tenant_id against
    # the SSO identity's bound tenant; mismatch is a 403 (the user
    # cannot kick off provisioning against someone else's tenant).
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "no_tenant_bound",
                "message": (
                    "Your session is not bound to a tenant yet. "
                    "Complete `/onboarding/select-region` first."
                ),
            },
        )
    if body.tenant_id != user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "tenant_mismatch",
                "message": (
                    "You cannot start a backfill for a tenant you do "
                    "not belong to."
                ),
            },
        )

    # 2. Tenant must exist and be active.
    binding = _fetch_user_tenant(user)
    if binding is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "tenant_not_found",
                "message": (
                    "No matching tenant row. Your session may be "
                    "stale — sign in again."
                ),
            },
        )
    tenant_id, active = binding
    if not active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "tenant_inactive",
                "message": (
                    "Tenant is inactive; reactivate before scheduling "
                    "a backfill."
                ),
            },
        )

    # 3. Idempotency: if a backfill has already been enqueued, return
    # the same id. The frontend's "refresh mid-poll" path needs this.
    with SessionLocal() as s:
        existing_id = s.execute(
            sql_text(
                "SELECT initial_backfill_task_id FROM tenants "
                "WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        ).scalar()
    if existing_id:
        return StartBackfillResponse(task_id=existing_id)

    # 4. Dispatch via the injectable seam. The default returns the
    # live Celery AsyncResult; tests substitute a stub.
    async_result = _task_dispatcher(tenant_id, body.days)
    task_id = getattr(async_result, "id", None)
    if not task_id:
        # Defensive: a dispatcher that returns something without `.id`
        # is a configuration bug — surface it instead of returning a
        # response the frontend can't poll.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "internal_error",
                "message": (
                    "Backfill task dispatcher returned no task id. "
                    "Contact support."
                ),
            },
        )

    # 5. Record the task id on the tenant row. Bootstrap role: tenants
    # is REVOKE'd from vargate_app per T3.4.
    with SessionLocal() as s:
        s.execute(
            sql_text(
                "UPDATE tenants SET initial_backfill_task_id = :tid, "
                "                    updated_at = NOW() "
                "WHERE tenant_id = :t"
            ),
            {"tid": task_id, "t": tenant_id},
        )
        s.commit()

    return StartBackfillResponse(task_id=task_id)


# ── GET /onboarding/backfill-status/{task_id} ──────────────────────────────


@router.get(
    "/onboarding/backfill-status/{task_id}",
    response_model=BackfillStatusResponse,
    operation_id="getBackfillStatus",
    tags=["onboarding"],
    summary="Poll the backfill task's progress",
)
def get_backfill_status(
    task_id: str = Path(..., description="Task id from start-backfill."),
    user: AuthenticatedUser = Depends(current_user),
) -> BackfillStatusResponse:
    """Reads Celery state for `task_id`, scoped to the user's tenant.

    A task id that isn't recorded against the user's tenant returns
    404 — this prevents (a) probing for foreign tenants' task ids
    and (b) Celery's PENDING-vs-unknown ambiguity (Celery treats
    "task id never existed" the same as "queued but not picked up").
    """
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "no_tenant_bound",
                "message": (
                    "Your session is not bound to a tenant yet. "
                    "Complete `/onboarding/select-region` first."
                ),
            },
        )

    # 1. Tenant-scope the task id. SELECT initial_backfill_task_id
    # WHERE tenant_id = user's tenant; only match-on-equal is OK.
    with SessionLocal() as s:
        recorded_id = s.execute(
            sql_text(
                "SELECT initial_backfill_task_id FROM tenants "
                "WHERE tenant_id = :t"
            ),
            {"t": user.tenant_id},
        ).scalar()
    if not recorded_id or recorded_id != task_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "task_not_found",
                "message": (
                    "No backfill task with that id is recorded against "
                    "your tenant."
                ),
            },
        )

    # 2. Read Celery state.
    result = _async_result_factory(task_id)
    state = result.state or "PENDING"
    info = result.info  # alias for `.result`

    # 3. Translate to the contract. PROGRESS carries the meta dict
    # the task emitted; SUCCESS carries the task's return value
    # (which also has chunks_processed / inserted / deduped); FAILURE
    # carries the exception; PENDING / STARTED have no detail to add.
    payload: dict[str, Any] = {"state": state}

    if state == "PROGRESS" and isinstance(info, dict):
        payload["chunks_processed"] = int(info.get("chunks_processed", 0))
        payload["inserted"] = int(info.get("inserted", 0))
        payload["deduped"] = int(info.get("deduped", 0))
    elif state == "SUCCESS" and isinstance(info, dict):
        payload["chunks_processed"] = int(info.get("chunks_processed", 0))
        payload["inserted"] = int(info.get("inserted", 0))
        payload["deduped"] = int(info.get("deduped", 0))
    elif state == "FAILURE":
        payload["error"] = _format_failure(info)

    return BackfillStatusResponse(**payload)


# ───────────────────────────────────────────────────────────────────────────
# TM2 Phase D1 — GET /onboarding/mcp-status
# ───────────────────────────────────────────────────────────────────────────
#
# The onboarding SPA polls this endpoint after the user has clicked
# "I've completed setup" on the MCP-Connector card. It reflects
# whether any `mcp` row has landed for the signed-in user's tenant.
# Same source of truth as the `mcp_connector` capability bool
# (test_mcp_capability.py) — just exposed with timestamp + count
# so the SPA can render "Connected — N interactions captured".


class McpStatusResponse(BaseModel):
    """Snapshot of MCP-connector ingest state for the caller's tenant.

    All three fields update independently of the configured-at
    timestamp on any DCR row — capability lights up by USAGE, not
    by registration. See the `mcp_connector` capability detector
    in this same module.
    """

    configured: bool = Field(
        ...,
        description=(
            "True iff at least one telemetry_records row with "
            "source_api='mcp' exists for this tenant in the last "
            "90 days. Same condition as the capability bool."
        ),
    )
    first_event_at: Optional[datetime] = Field(
        default=None,
        description=(
            "ISO-8601 UTC timestamp of the earliest mcp record "
            "for this tenant. Null if no events yet."
        ),
    )
    events_count: int = Field(
        ...,
        description=(
            "Total mcp records in the last 90 days. The SPA's poll "
            "loop watches for this to transition from 0 → N."
        ),
    )


@router.get(
    "/onboarding/mcp-status",
    response_model=McpStatusResponse,
    operation_id="getMcpStatus",
    tags=["onboarding"],
    summary="MCP-connector ingest status for the caller's tenant",
)
def get_mcp_status(
    user: AuthenticatedUser = Depends(current_user),
) -> McpStatusResponse:
    """Return the MCP-source ingest snapshot.

    A pre-select-region caller (no bound tenant_id yet) gets the
    not-configured shape — they can't have any MCP traffic by
    construction.
    """
    if not user.tenant_id:
        return McpStatusResponse(
            configured=False, first_event_at=None, events_count=0
        )

    # Use the session-scope so RLS applies — the COUNT + MIN are
    # cheap enough to run per poll without caching, especially with
    # the (tenant_id, occurred_at) index that lands on
    # telemetry_records via T2.1's ix_telemetry_records_tenant_occurred.
    from vargate_telemetry.db import session_scope

    with session_scope(user.tenant_id) as s:
        row = s.execute(
            sql_text(
                "SELECT MIN(occurred_at) AS first_at, "
                "       COUNT(*) AS n "
                "FROM telemetry_records "
                "WHERE tenant_id = :t "
                "  AND source_api = 'mcp' "
                "  AND ingested_at > now() - INTERVAL '90 days'"
            ),
            {"t": user.tenant_id},
        ).one()

    count = int(row.n or 0)
    return McpStatusResponse(
        configured=count > 0,
        first_event_at=row.first_at if count > 0 else None,
        events_count=count,
    )
