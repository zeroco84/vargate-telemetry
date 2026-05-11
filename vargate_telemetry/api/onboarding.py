# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Onboarding routes — key validation, region select, backfill kick-off.

T4.4 ships `POST /onboarding/validate-key`. T4.5 adds
`POST /onboarding/select-region`. T4.6 will add `start-backfill` +
`backfill-status`.

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
from typing import Callable, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text

from vargate_telemetry.anthropic import AnthropicAdminClient
from vargate_telemetry.anthropic.exceptions import (
    AnthropicAPIError,
    RateLimited,
)
from vargate_telemetry.auth.jwt import (
    SESSION_COOKIE_NAME,
    SESSION_TOKEN_TTL_SECONDS,
    issue_session_jwt,
)
from vargate_telemetry.auth.middleware import AuthenticatedUser, current_user
from vargate_telemetry.crypto.dek import encrypt_with_dek, generate_dek
from vargate_telemetry.crypto.hsm import wrap_dek
from vargate_telemetry.crypto.integrity import compute_integrity_tag
from vargate_telemetry.db import SessionLocal


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
    admin_api: bool
    compliance_api: bool
    code_analytics: bool


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


def _is_auth_failure(exc: BaseException) -> bool:
    """True iff this exception is a 401/403 from Anthropic.

    Both `AnthropicAdminClient` (5xx → AnthropicAPIError) and httpx
    (4xx → HTTPStatusError) feed into the same set of "the credential
    is bad" symptoms. Catch both shapes here so the call sites stay
    short.
    """
    if isinstance(exc, AnthropicAPIError):
        return False  # AnthropicAPIError only fires for 5xx
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

    # 2. Members probe — used here as a loose proxy for Compliance
    # API capability until T5 lands the real Compliance probe. If
    # the admin probe worked but this one doesn't, the user's key
    # has Admin scope but not the broader Compliance scope.
    try:
        next(iter(client.list_members()), None)
        compliance_api_ok = True
    except Exception as exc:
        if _is_auth_failure(exc):
            compliance_api_ok = False
        else:
            # A 5xx from Anthropic is not the user's fault — but it
            # also doesn't tell us much about scope. Default to
            # `False` here so the UI doesn't claim Compliance is
            # available when we couldn't confirm.
            compliance_api_ok = False

    # 3. Code Analytics: real probe lands in T5; for T4.4 default to
    # `False` so the UI can render the "missing capability" row.
    code_analytics_ok = False

    return ValidateKeyResponse(
        org_name=org_name or "Your Anthropic Organization",
        capabilities=KeyCapabilities(
            admin_api=admin_api_ok,
            compliance_api=compliance_api_ok,
            code_analytics=code_analytics_ok,
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

        # ───── 5. UPDATE users.tenant_id (bootstrap role) ────────────────
        s.execute(
            sql_text("UPDATE users SET tenant_id = :t WHERE id = :uid"),
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
