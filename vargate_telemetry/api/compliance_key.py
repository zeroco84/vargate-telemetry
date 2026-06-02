# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Compliance Access Key onboarding (TM5 T5.1).

`POST /onboarding/compliance-key` — the post-onboarding step that
collects a tenant's **Compliance Access Key** (``sk-ant-api01-...``), a
*separate* key type from the Admin API key (``sk-ant-admin01-...``)
sealed at first onboarding. The compliance key is what unlocks
**content capture** (chats + message text); the admin key only reaches
usage aggregates + the Activity Feed.

Flow (validate → seal → flip capability):

  1. **Format guard** (local, no network). Reject a mis-pasted Admin key
     (``sk-ant-admin01-``) with a pointed message, and anything that
     isn't ``sk-ant-api01-``.
  2. **Probe** the live Compliance API to confirm the key actually
     reaches content. The probe walks the head of the content-capture
     enumeration chain — ``list_organizations`` then
     ``list_organization_users`` — because content capture needs BOTH
     scopes: ``read:compliance_org_data`` (to enumerate orgs → get the
     ``org_uuid``) and ``read:compliance_user_data`` (to enumerate users
     → get the ``user_ids[]`` the chats endpoint requires). A single
     probe against either endpoint alone would let a half-scoped key
     pass and then fail at content-pull time. ``list_activities`` is NOT
     used here: an Admin key can reach the Activity Feed too, so it
     can't prove the key is a content-capable Compliance Access Key.
  3. **Seal** via ``seal_secret`` (UPSERT — re-submitting rotates the
     key) under the canonical name ``anthropic_compliance_access_key``.
  4. ``content_capture`` in ``GET /me/capabilities`` flips True off the
     mere presence of that sealed secret (see ``api/auth.py``).

**Build-blind (TM5):** there is no Compliance Access Key to test
against yet (sandbox key pending from Anthropic, no timeline). This
endpoint is built + unit-tested against the documented contract
(reconned 2026-06-02) + mocked client responses; the live-verify is
deferred (Track-D-D4 style). When a real key lands, the live close-out
is: provision a both-scope key on an Enterprise org → submit it here →
confirm content_capture flips + T5.2's content pull starts succeeding.

Admin-gated (``require_admin``, TM4): sealing tenant credentials is an
admin action, same posture as budget writes + alias mapping.
"""

from __future__ import annotations

import logging
from itertools import islice
from typing import Any, Callable, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from vargate_telemetry.anthropic import (
    ANTHROPIC_COMPLIANCE_KEY_SECRET,
    AnthropicAdminClient,
    AnthropicAPIError,
    InsufficientScope,
    RateLimited,
)
from vargate_telemetry.auth.middleware import AuthenticatedUser
from vargate_telemetry.auth.roles import require_admin
from vargate_telemetry.crypto.seal import seal_secret

_log = logging.getLogger(__name__)

router = APIRouter()


# ───────────────────────────────────────────────────────────────────────────
# Request / response shapes
# ───────────────────────────────────────────────────────────────────────────


class ComplianceKeyRequest(BaseModel):
    compliance_key: str = Field(
        ...,
        min_length=20,
        description=(
            "Anthropic Compliance Access Key (begins `sk-ant-api01-`), "
            "created in claude.ai by an Enterprise owner. Must carry both "
            "`read:compliance_org_data` and `read:compliance_user_data` "
            "scopes — content capture enumerates orgs → users → chats."
        ),
    )


class ComplianceKeyResponse(BaseModel):
    content_capture: bool = Field(
        ...,
        description="True once the key is validated + sealed.",
    )
    org_name: Optional[str] = Field(
        None,
        description=(
            "Name of the first organization the key sees, echoed back as "
            "a confirmation. Null if the org tree is empty."
        ),
    )


# ───────────────────────────────────────────────────────────────────────────
# Client-factory injection point (mirrors onboarding.set_client_factory_for_test).
# Tests substitute a stub; production builds a live AnthropicAdminClient
# carrying the candidate compliance key in its x-api-key header.
# ───────────────────────────────────────────────────────────────────────────


ComplianceClientFactory = Callable[[str], AnthropicAdminClient]


def _default_compliance_client_factory(key: str) -> AnthropicAdminClient:
    """Live client for the candidate key. Short waits keep the probe snappy
    even if Anthropic returns a transient 429."""
    return AnthropicAdminClient(api_key=key, min_wait=1.0, max_wait=8.0)


_compliance_client_factory: ComplianceClientFactory = (
    _default_compliance_client_factory
)


def set_compliance_client_factory_for_test(
    factory: Optional[ComplianceClientFactory],
) -> None:
    """Substitute a client factory for tests. Pass ``None`` to reset."""
    global _compliance_client_factory
    _compliance_client_factory = (
        factory if factory is not None else _default_compliance_client_factory
    )


# ───────────────────────────────────────────────────────────────────────────
# Error messages + probe-failure mapping
# ───────────────────────────────────────────────────────────────────────────


_ADMIN_KEY_PASTED_MESSAGE = (
    "That looks like an Admin API key (`sk-ant-admin01-…`). Content "
    "capture needs a separate Compliance Access Key (`sk-ant-api01-…`), "
    "created in claude.ai under an Enterprise organization. The Admin key "
    "you already connected stays in place for usage + activity data."
)

_MALFORMED_KEY_MESSAGE = (
    "That doesn't look like a Compliance Access Key. Expected a key that "
    "begins `sk-ant-api01-`, created in claude.ai (Settings → Compliance "
    "API) by an Enterprise owner."
)

_INVALID_KEY_MESSAGE = (
    "Anthropic rejected this Compliance Access Key. Re-issue it from "
    "claude.ai and try again."
)


def _insufficient_scope_message(scope: str) -> str:
    return (
        f"The key reached Anthropic but can't read compliance data — it's "
        f"missing the `{scope}` scope, or your organization's plan doesn't "
        f"include the Compliance API (Enterprise only). Re-create the "
        f"Compliance Access Key in claude.ai with BOTH "
        f"`read:compliance_org_data` and `read:compliance_user_data` scopes."
    )


def _rate_limited(exc: RateLimited) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "anthropic_rate_limited",
            "message": (
                f"Anthropic rate-limited the validation probe; retry in "
                f"{exc.retry_after}s."
            ),
        },
    )


def _probe_failure(exc: BaseException, *, scope: str) -> HTTPException:
    """Map a probe exception to the right client-facing HTTPException.

    Order matters — ``InsufficientScope`` is an ``AnthropicAPIError``
    subclass (403), so check it before the generic 5xx branch.
    """
    if isinstance(exc, InsufficientScope):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "insufficient_scope",
                "message": _insufficient_scope_message(scope),
            },
        )
    if (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code in (401, 403)
    ):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_compliance_key",
                "message": _INVALID_KEY_MESSAGE,
            },
        )
    if isinstance(exc, AnthropicAPIError):
        # 5xx from Anthropic — not the key's fault, and we couldn't
        # confirm the key. Don't seal; ask the user to retry.
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "anthropic_error",
                "message": (
                    "Anthropic returned an error while validating the key. "
                    "Try again shortly."
                ),
            },
        )
    # Network failure / unexpected — surface as a transient gateway error.
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "code": "probe_failed",
            "message": (
                "Couldn't reach Anthropic to validate the key. Try again "
                "shortly."
            ),
        },
    )


# ───────────────────────────────────────────────────────────────────────────
# POST /onboarding/compliance-key
# ───────────────────────────────────────────────────────────────────────────


@router.post(
    "/onboarding/compliance-key",
    response_model=ComplianceKeyResponse,
    operation_id="submitComplianceKey",
    tags=["onboarding"],
    summary="Validate + seal a Compliance Access Key (enables content capture)",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": (
                "Key is malformed, the wrong key type, rejected by "
                "Anthropic, or missing a required compliance scope."
            ),
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller is not an admin of their tenant.",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Anthropic rate-limited the validation probe.",
        },
    },
)
def submit_compliance_key(
    body: ComplianceKeyRequest,
    user: AuthenticatedUser = Depends(require_admin),
) -> ComplianceKeyResponse:
    """Validate a Compliance Access Key against the live Compliance API,
    then seal it for the caller's tenant.

    ``require_admin`` guarantees a bound tenant + admin role. The key is
    only sealed after BOTH probes pass — a bad/half-scoped key 400s
    without touching ``encrypted_secrets``, so there's no partial state.
    """
    tenant_id = user.tenant_id  # require_admin guarantees non-None
    assert tenant_id is not None  # for type-checkers; require_admin enforces

    key = body.compliance_key.strip()

    # 1. Format guard — catch the common mistake (pasting the Admin key)
    #    and obvious garbage before spending a network round-trip.
    if key.startswith("sk-ant-admin01-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "wrong_key_type",
                "message": _ADMIN_KEY_PASTED_MESSAGE,
            },
        )
    if not key.startswith("sk-ant-api01-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "malformed_compliance_key",
                "message": _MALFORMED_KEY_MESSAGE,
            },
        )

    # 2. Probe: walk orgs → users to confirm BOTH scopes content capture
    #    needs. Each probe pulls exactly one item (limit=1 / islice) so
    #    the cost is two cheap calls.
    org_name: Optional[str] = None
    client = _compliance_client_factory(key)
    try:
        # 2a. read:compliance_org_data — also yields the org_uuid for 2b.
        try:
            orgs = list(islice(client.list_organizations(), 1))
        except RateLimited as exc:
            raise _rate_limited(exc) from exc
        except Exception as exc:
            raise _probe_failure(
                exc, scope="read:compliance_org_data"
            ) from exc

        if orgs:
            org_name = orgs[0].name
            # 2b. read:compliance_user_data — the scope chats actually need.
            try:
                next(
                    iter(
                        client.list_organization_users(
                            orgs[0].uuid, limit=1
                        )
                    ),
                    None,
                )
            except RateLimited as exc:
                raise _rate_limited(exc) from exc
            except Exception as exc:
                raise _probe_failure(
                    exc, scope="read:compliance_user_data"
                ) from exc
        else:
            # Key authenticated for org-data but the tree is empty — no
            # org to probe users against. Unusual for a real Enterprise
            # key (always bound to ≥1 org). Seal anyway (the key is
            # valid); the user-scope check happens at first content pull.
            _log.warning(
                "compliance-key: tenant %s key authenticated but the org "
                "tree is empty; sealing without a user-scope probe",
                tenant_id,
            )
    finally:
        client.close()

    # 3. Seal (UPSERT — re-submitting rotates the key in place).
    try:
        seal_secret(
            tenant_id,
            ANTHROPIC_COMPLIANCE_KEY_SECRET,
            key.encode("utf-8"),
        )
    except LookupError as exc:
        # No DEK provisioned — a fully-onboarded tenant always has one,
        # so this is a "shouldn't happen" provisioning gap, not user error.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "tenant_not_provisioned",
                "message": (
                    "Your tenant isn't fully provisioned yet. Finish "
                    "onboarding (region select) before connecting a "
                    "Compliance Access Key."
                ),
            },
        ) from exc

    _log.info(
        "compliance-key: sealed for tenant %s — content_capture enabled",
        tenant_id,
    )
    return ComplianceKeyResponse(content_capture=True, org_name=org_name)
