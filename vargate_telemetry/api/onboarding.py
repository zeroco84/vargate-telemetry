# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Onboarding routes — key validation, region select, backfill kick-off.

T4.4 ships `POST /onboarding/validate-key` only. T4.5 adds
`select-region`, T4.6 adds `start-backfill` + `backfill-status`.

`validate-key` is the live probe the UI uses to gate the "Continue"
button on the paste-key screen. It builds an EPHEMERAL
`AnthropicAdminClient` from the supplied key (no persistence —
the key only gets sealed in T4.5 after region confirmation),
calls `list_workspaces()` and `list_members()` against Anthropic,
and returns a structured capability report:

  - admin_api      — `list_workspaces()` succeeded
  - compliance_api — `list_members()` succeeded (loose proxy until
                     T5 lands the actual Compliance API probe)
  - code_analytics — always false in T4.4; T5 wires the real probe

org_name is the first workspace's name (or a stable placeholder
when an org has no workspaces). The UI uses it to show "you're
about to onboard Acme Corp" — a visual confirmation that the user
pasted the right key.

**The endpoint never writes to the database.** Tested explicitly
via `test_validate_key_does_not_persist_anything`.
"""

from __future__ import annotations

from typing import Callable, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from vargate_telemetry.anthropic import AnthropicAdminClient
from vargate_telemetry.anthropic.exceptions import (
    AnthropicAPIError,
    RateLimited,
)
from vargate_telemetry.auth.middleware import AuthenticatedUser, current_user


router = APIRouter()


# ───────────────────────────────────────────────────────────────────────────
# Pydantic shapes — match the YAML's ValidateKeyRequest / KeyCapabilities /
# ValidateKeyResponse.
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
# The route
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
