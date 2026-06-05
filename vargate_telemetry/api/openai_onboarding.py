# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""OpenAI Admin Key onboarding (TM8 Phase C).

Adds an **OpenAI Admin API key** (``sk-admin-…``) to an
already-provisioned tenant, alongside the Anthropic key sealed at first
onboarding. OpenAI is Ogma's first non-Anthropic vendor — this is the
second-vendor card on the onboarding/Settings → Integrations grid, NOT a
tenant-creating flow. The tenant already has a DEK (from the Anthropic
``select-region`` step); we seal the OpenAI key under that same DEK.

Two routes, mirroring the Anthropic validate-then-seal separation
(``/onboarding/validate-key`` + the compliance-key submit), namespaced
under ``/onboarding/openai/`` so the frontend mirrors them cleanly:

  1. ``POST /onboarding/openai/validate-key`` — the **probe** step. Builds
     an EPHEMERAL ``OpenAIAdminClient`` from the supplied key and probes
     each org endpoint family, returning a capability checklist:

         {valid, org_id, capabilities: {
            admin, costs, audit_logs, project_users, per_user_breakdown}}

     It does NOT seal the key. A rejected key (401/403) returns
     ``valid: false`` with a human ``reason`` — **never a 500** — so the
     UI renders a "this key doesn't work" state without an error toast.
     The checklist field names line up 1:1 with the ``openai`` block of
     ``GET /me/capabilities`` (see ``api/auth.py``) so the same UI matrix
     renders both.

  2. ``POST /onboarding/openai/submit`` — the **seal + backfill** step.
     Admin-gated (``require_admin``, same posture as the compliance-key
     and budget writes). Re-probes defensively (a key that 403s here is
     not sealed), seals the key under ``OPENAI_ADMIN_KEY_SECRET``
     (``"openai_admin_key"`` — UPSERT, so re-submitting rotates), then
     enqueues the historical backfill: the per-tenant usage / costs /
     projects pull tasks. OpenAI has no EU residency, so the region is
     always ``"us"``. Returns the sealed flag, the region, the probe
     checklist, and the names of the enqueued streams.

Probe-to-capability mapping (recon §5, ``TM8-openai-recon.md``):

  - ``admin``               ← ``GET /usage/completions`` → 200
  - ``costs``               ← ``GET /costs`` → 200
  - ``audit_logs``          ← ``GET /audit_logs`` → 200 (**accessible**,
                              even when empty below Enterprise — the
                              probe can't see future rows, so this is
                              "reachable", matching the validate-key
                              ``activity_feed`` posture, NOT the
                              data-layer "populated" capability)
  - ``project_users``       ← ``GET /users`` → 200 with ≥1 row
  - ``per_user_breakdown``  ← ``GET /usage/completions`` with
                              ``group_by=user_id`` returns a row whose
                              ``user_id`` is non-null (PAYG already
                              populates this, recon §2)

Build posture: built + unit-tested against a ``MockTransport``-backed
client (the ``set_openai_client_factory_for_test`` seam) + the documented
recon contract. The live close-out is a real ``sk-admin-`` key submitted
here → ``admin`` flips in ``/me/capabilities`` → the OpenAI pulls start
landing rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from itertools import islice
from typing import Callable, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from vargate_telemetry.auth.middleware import AuthenticatedUser, current_user
from vargate_telemetry.auth.roles import require_admin
from vargate_telemetry.crypto.seal import seal_secret
from vargate_telemetry.openai import (
    InsufficientScope,
    OpenAIAdminClient,
    OpenAIAPIError,
    RateLimited,
)
from vargate_telemetry.openai.factory import OPENAI_ADMIN_KEY_SECRET

_log = logging.getLogger(__name__)

router = APIRouter()


# ───────────────────────────────────────────────────────────────────────────
# Request / response shapes — match openapi/ogma-api.yaml:
#   OpenAIValidateKeyRequest / OpenAIKeyCapabilities /
#   OpenAIValidateKeyResponse / OpenAISubmitResponse
# ───────────────────────────────────────────────────────────────────────────


class OpenAIValidateKeyRequest(BaseModel):
    admin_key: str = Field(
        ...,
        min_length=20,
        description="OpenAI Admin API key (begins `sk-admin-`).",
    )


class OpenAIKeyCapabilities(BaseModel):
    """Per-key capability checklist — field names mirror the ``openai``
    block of ``GET /me/capabilities`` so one UI matrix renders both.

    Each bool is a read-only probe against the matching org endpoint:

      - ``admin``              — ``/usage/completions`` reachable (200).
      - ``costs``              — ``/costs`` reachable (200).
      - ``audit_logs``         — ``/audit_logs`` reachable (200). NOTE
        this is *accessibility*, not population: the endpoint 200s but is
        empty below Enterprise (recon §4). The data-layer capability in
        ``/me/capabilities`` stays False until a row actually lands; the
        onboarding checklist reports reachability so the user sees the key
        works.
      - ``project_users``      — ``/users`` returned ≥1 row.
      - ``per_user_breakdown`` — a ``group_by=user_id`` usage row carried
        a non-null ``user_id`` (drives the honest empty-state copy; PAYG
        already populates this, recon §2).
    """

    admin: bool
    costs: bool
    audit_logs: bool
    project_users: bool
    per_user_breakdown: bool


class OpenAIValidateKeyResponse(BaseModel):
    valid: bool = Field(
        ...,
        description=(
            "True iff the key authenticated against the OpenAI Admin API "
            "(at least the usage endpoint reachable). False — with a "
            "`reason` — when OpenAI rejected the key (401/403); the "
            "endpoint never 500s on a bad key."
        ),
    )
    org_id: Optional[str] = Field(
        None,
        description=(
            "The OpenAI organization id the key belongs to "
            "(`org-…`), echoed back as a confirmation. Resolved from the "
            "costs probe (the only endpoint that returns `organization_id` "
            "— recon §3); null if costs is unreachable or empty."
        ),
    )
    reason: Optional[str] = Field(
        None,
        description=(
            "Human-readable explanation when `valid` is false. Null on "
            "success."
        ),
    )
    capabilities: OpenAIKeyCapabilities


class OpenAISubmitResponse(BaseModel):
    sealed: bool = Field(
        ...,
        description="True once the key is validated + sealed for the tenant.",
    )
    region: str = Field(
        ...,
        description=(
            "Always `us` — OpenAI exposes no EU data-residency region "
            "(recon §5). Echoed for symmetry with the Anthropic flow."
        ),
    )
    org_id: Optional[str] = Field(
        None,
        description="The OpenAI organization id the key belongs to.",
    )
    capabilities: OpenAIKeyCapabilities
    backfill_enqueued: list[str] = Field(
        default_factory=list,
        description=(
            "The OpenAI ingest streams whose backfill pulls were enqueued "
            "on submit, e.g. "
            "`['openai_admin_usage', 'openai_admin_costs', "
            "'openai_projects']`. The 15-minute beat picks the streams up "
            "thereafter; these one-shot dispatches seed the dashboard "
            "immediately."
        ),
    )


# ───────────────────────────────────────────────────────────────────────────
# Client-factory injection point (mirrors onboarding.set_client_factory_for_test
# / compliance_key.set_compliance_client_factory_for_test).
#
# Tests substitute a MockTransport-backed OpenAIAdminClient; production
# builds a live one carrying the candidate key as a Bearer header.
# ───────────────────────────────────────────────────────────────────────────


OpenAIClientFactory = Callable[[str], OpenAIAdminClient]


def _default_openai_client_factory(admin_key: str) -> OpenAIAdminClient:
    """Build the live probe client. Short waits keep the validate-key
    response snappy even if OpenAI returns a transient 429."""
    return OpenAIAdminClient(api_key=admin_key, min_wait=1.0, max_wait=8.0)


_openai_client_factory: OpenAIClientFactory = _default_openai_client_factory


def set_openai_client_factory_for_test(
    factory: Optional[OpenAIClientFactory],
) -> None:
    """Substitute the probe-client factory for tests. Pass ``None`` to reset."""
    global _openai_client_factory
    _openai_client_factory = (
        factory if factory is not None else _default_openai_client_factory
    )


# ───────────────────────────────────────────────────────────────────────────
# Backfill-dispatch injection point.
#
# Production fans out the live per-tenant pull tasks via ``.delay``; tests
# substitute a recorder so we can assert which streams were enqueued
# without standing up a Celery worker. Same posture as onboarding's
# ``set_task_dispatcher_for_test``.
# ───────────────────────────────────────────────────────────────────────────


BackfillDispatcher = Callable[[str], list[str]]
"""(tenant_id) -> the list of stream names that were enqueued."""


# The streams seeded on submit. ``audit_logs`` is intentionally NOT
# enqueued here: it's empty below Enterprise (recon §4), so a one-shot
# backfill pull would land nothing — the hourly beat covers it for the
# rare populated org. usage + costs + projects are what light up the
# dashboard for the just-onboarded tenant.
BACKFILL_STREAMS = [
    "openai_admin_usage",
    "openai_admin_costs",
    "openai_projects",
]


def _default_backfill_dispatcher(tenant_id: str) -> list[str]:
    """Enqueue the one-shot backfill pulls for a freshly-sealed key.

    Dispatches the per-tenant usage / costs / projects pull tasks (each a
    cursor-driven lookback that, on first run with no cursor, pulls the
    initial window and advances). Imported lazily so importing this route
    module doesn't pull the whole Celery task graph at app-import time
    (matches the onboarding.py late-import posture).
    """
    from vargate_telemetry.tasks.pull_openai_costs import (
        pull_openai_costs_for_tenant,
    )
    from vargate_telemetry.tasks.pull_openai_projects import (
        pull_openai_projects_for_tenant,
    )
    from vargate_telemetry.tasks.pull_openai_usage import (
        pull_openai_usage_for_tenant,
    )

    # Projects first so the openai_users side table (email map) is
    # populated before the usage pull resolves user_id → email; both are
    # idempotent and the usage pull degrades gracefully if it races ahead
    # (it just lands the raw user_id and the next pull fills the email).
    pull_openai_projects_for_tenant.delay(tenant_id)
    pull_openai_usage_for_tenant.delay(tenant_id)
    pull_openai_costs_for_tenant.delay(tenant_id)
    return list(BACKFILL_STREAMS)


_backfill_dispatcher: BackfillDispatcher = _default_backfill_dispatcher


def set_backfill_dispatcher_for_test(
    dispatcher: Optional[BackfillDispatcher],
) -> None:
    """Substitute the backfill dispatcher for tests. Pass ``None`` to reset."""
    global _backfill_dispatcher
    _backfill_dispatcher = (
        dispatcher if dispatcher is not None else _default_backfill_dispatcher
    )


# ───────────────────────────────────────────────────────────────────────────
# Error messages
# ───────────────────────────────────────────────────────────────────────────


_INVALID_KEY_REASON = (
    "OpenAI rejected this Admin key. Create one at platform.openai.com → "
    "Settings → Admin keys → Create, and make sure it begins `sk-admin-`."
)

_WRONG_KEY_TYPE_REASON = (
    "That looks like a standard OpenAI API key (`sk-…`), not an "
    "organization Admin key. Ogma needs an Admin key (`sk-admin-…`), "
    "created at platform.openai.com → Settings → Admin keys."
)


def _rate_limited(exc: RateLimited) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "openai_rate_limited",
            "message": (
                f"OpenAI rate-limited the validation probe; retry in "
                f"{exc.retry_after}s."
            ),
        },
    )


# ───────────────────────────────────────────────────────────────────────────
# Probe — shared by validate-key and submit.
# ───────────────────────────────────────────────────────────────────────────


# A short window for the probe usage/costs calls — one day back, today.
# Keeps the probe cheap (one bucket) while still exercising the real
# endpoints. ``/costs`` is slow (~5s, recon §5) but a one-bucket probe is
# the floor.
_PROBE_LOOKBACK_DAYS = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _ProbeResult:
    """Internal carrier for a probe pass — the checklist plus org_id, plus
    a ``hard_auth_failure`` flag set when the *admin* probe itself 401/403s
    (i.e. the key is unusable, vs a single sub-endpoint being scope-gated).
    """

    def __init__(self) -> None:
        self.admin = False
        self.costs = False
        self.audit_logs = False
        self.project_users = False
        self.per_user_breakdown = False
        self.org_id: Optional[str] = None
        self.hard_auth_failure = False
        self.rate_limited: Optional[RateLimited] = None

    def to_capabilities(self) -> OpenAIKeyCapabilities:
        return OpenAIKeyCapabilities(
            admin=self.admin,
            costs=self.costs,
            audit_logs=self.audit_logs,
            project_users=self.project_users,
            per_user_breakdown=self.per_user_breakdown,
        )


def _probe_openai_key(client: OpenAIAdminClient) -> _ProbeResult:
    """Probe each org endpoint family; build the capability checklist.

    Each probe consumes exactly one item (``islice(..., 1)``) so the cost
    is one cheap call per endpoint. A 403 on a single sub-endpoint marks
    that one capability False without failing the whole probe — only a
    403/401 on the *admin* (usage) probe is a hard auth failure (the key
    is unusable). A 429 anywhere short-circuits and is surfaced as a 503
    by the callers.

    Never raises for an auth/scope failure (401 or 403) — those are
    recorded on the result so neither route 500s on a bad key.
    ``RateLimited`` is recorded (callers translate to 503); an unexpected
    ``OpenAIAPIError`` (5xx) or a non-auth native httpx error propagates so
    a genuine outage isn't silently reported as an invalid key.
    """
    result = _ProbeResult()
    window_end = _now()
    window_start = window_end - timedelta(days=_PROBE_LOOKBACK_DAYS)

    # 1. admin — /usage/completions reachable. Request group_by=user_id so
    #    the same single call also tells us per_user_breakdown.
    try:
        first = next(
            iter(
                client.list_usage(
                    start_time=window_start,
                    end_time=window_end,
                    group_by=["user_id"],
                    limit=1,
                )
            ),
            None,
        )
        result.admin = True
        if first is not None and first.results:
            result.per_user_breakdown = any(
                getattr(r, "user_id", None) for r in first.results
            )
    except RateLimited as exc:
        result.rate_limited = exc
        return result
    except Exception as exc:
        # The key can't even read usage → unusable. A 401/403 is a hard
        # auth failure (valid:false / 400); anything else (5xx, network)
        # propagates so a real outage isn't mislabeled as a bad key.
        if _is_auth_failure(exc):
            result.hard_auth_failure = True
            return result
        raise

    # 2. costs — /costs reachable. Also the only endpoint that returns the
    #    organization_id (recon §3), so we harvest org_id here.
    try:
        cost_bucket = next(
            iter(
                client.list_costs(
                    start_time=window_start,
                    end_time=window_end,
                    group_by=["project_id", "line_item"],
                    limit=1,
                )
            ),
            None,
        )
        result.costs = True
        if cost_bucket is not None:
            for row in getattr(cost_bucket, "results", None) or ():
                org_id = getattr(row, "organization_id", None)
                if org_id:
                    result.org_id = org_id
                    break
    except RateLimited as exc:
        result.rate_limited = exc
        return result
    except Exception as exc:
        # A scope-limited key 401/403s a sub-endpoint without invalidating
        # the whole key — that capability is just False. Re-raise a 5xx /
        # network error.
        if not _is_auth_failure(exc):
            raise
        result.costs = False

    # 3. audit_logs — reachable (accessible, may be empty below Enterprise).
    try:
        next(iter(islice(client.list_audit_logs(limit=1), 1)), None)
        result.audit_logs = True
    except RateLimited as exc:
        result.rate_limited = exc
        return result
    except Exception as exc:
        if not _is_auth_failure(exc):
            raise
        result.audit_logs = False

    # 4. project_users — /users returned at least one row.
    try:
        first_user = next(iter(islice(client.list_users(limit=1), 1)), None)
        result.project_users = first_user is not None
    except RateLimited as exc:
        result.rate_limited = exc
        return result
    except Exception as exc:
        if not _is_auth_failure(exc):
            raise
        result.project_users = False

    return result


def _is_auth_failure(exc: BaseException) -> bool:
    """True iff this exception is a credential rejection (401/403).

    The OpenAI client raises ``InsufficientScope`` (a typed 403) on 403,
    but a bare **401** surfaces as ``httpx.HTTPStatusError`` from
    ``raise_for_status()`` (the client only wraps 403/429/5xx). Both mean
    "this key can't reach the endpoint" for probe purposes — recognize
    both so a 401 doesn't escape as a 500 (the spec: never 500 on a bad
    key). A non-403 ``OpenAIAPIError`` (5xx) is NOT an auth failure.
    """
    if isinstance(exc, InsufficientScope):
        return True
    if isinstance(exc, OpenAIAPIError):
        return False  # 5xx — a genuine outage, not the key's fault
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (401, 403)
    return False


def _looks_like_admin_key(key: str) -> bool:
    """``sk-admin-`` prefix gate — catches the common mistake of pasting a
    standard project key (``sk-…`` / ``sk-proj-…``) before a network call."""
    return key.startswith("sk-admin-")


# ───────────────────────────────────────────────────────────────────────────
# POST /onboarding/openai/validate-key
# ───────────────────────────────────────────────────────────────────────────


@router.post(
    "/onboarding/openai/validate-key",
    response_model=OpenAIValidateKeyResponse,
    operation_id="validateOpenAIKey",
    tags=["onboarding"],
    summary="Validate an OpenAI Admin API key (probe only, no seal)",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "OpenAI rate-limited the validation probe.",
        },
    },
)
def validate_openai_key(
    body: OpenAIValidateKeyRequest,
    user: AuthenticatedUser = Depends(current_user),
) -> OpenAIValidateKeyResponse:
    """Probe OpenAI to determine which org endpoints the supplied key can
    reach. Returns the capability checklist + the org id.

    Does NOT seal the key — ``/onboarding/openai/submit`` owns the seal
    (admin-gated). A rejected key (401/403 on the admin probe) returns
    ``valid: false`` with a ``reason``, never a 500: a bad paste is a
    normal UI state, not a server error.

    Only requires a signed-in user (``current_user``) — the probe is a
    read-only OpenAI call with no tenant side effects, so a member can run
    it; the admin gate lands on submit.
    """
    key = body.admin_key.strip()

    # Format guard — a standard project key can't reach the org API.
    # Return valid:false (not a 400) so the UI renders the same "key
    # doesn't work" panel it uses for a rejected key, with a pointed
    # reason. No network round-trip spent.
    if not _looks_like_admin_key(key):
        return OpenAIValidateKeyResponse(
            valid=False,
            org_id=None,
            reason=_WRONG_KEY_TYPE_REASON,
            capabilities=OpenAIKeyCapabilities(
                admin=False,
                costs=False,
                audit_logs=False,
                project_users=False,
                per_user_breakdown=False,
            ),
        )

    client = _openai_client_factory(key)
    try:
        result = _probe_openai_key(client)
    finally:
        client.close()

    if result.rate_limited is not None:
        raise _rate_limited(result.rate_limited)

    if result.hard_auth_failure or not result.admin:
        return OpenAIValidateKeyResponse(
            valid=False,
            org_id=None,
            reason=_INVALID_KEY_REASON,
            capabilities=result.to_capabilities(),
        )

    return OpenAIValidateKeyResponse(
        valid=True,
        org_id=result.org_id,
        reason=None,
        capabilities=result.to_capabilities(),
    )


# ───────────────────────────────────────────────────────────────────────────
# POST /onboarding/openai/submit
# ───────────────────────────────────────────────────────────────────────────


@router.post(
    "/onboarding/openai/submit",
    response_model=OpenAISubmitResponse,
    operation_id="submitOpenAIKey",
    tags=["onboarding"],
    summary="Validate + seal an OpenAI Admin key and enqueue the backfill",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": (
                "Key is the wrong type (`code: wrong_key_type`) or was "
                "rejected by OpenAI (`code: invalid_openai_key`)."
            ),
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller is not an admin of their tenant.",
        },
        status.HTTP_409_CONFLICT: {
            "description": (
                "`code: tenant_not_provisioned` — the tenant has no DEK; "
                "finish Anthropic onboarding (region select) first."
            ),
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "OpenAI rate-limited the validation probe.",
        },
    },
)
def submit_openai_key(
    body: OpenAIValidateKeyRequest,
    user: AuthenticatedUser = Depends(require_admin),
) -> OpenAISubmitResponse:
    """Validate the key against the live OpenAI Admin API, seal it for the
    caller's tenant, then enqueue the historical backfill.

    ``require_admin`` guarantees a bound tenant + admin role. The key is
    only sealed after the admin probe passes — a bad/wrong key 400s
    without touching ``encrypted_secrets``, so there's no partial state.
    Re-submitting rotates the sealed key in place (UPSERT) and re-enqueues
    the backfill (each pull task is idempotent via the
    ``(tenant, source_api, external_id)`` dedup).

    Region is always ``"us"`` (OpenAI has no EU residency, recon §5).
    """
    tenant_id = user.tenant_id  # require_admin guarantees non-None
    assert tenant_id is not None  # for type-checkers; require_admin enforces

    key = body.admin_key.strip()

    # 1. Format guard — reject a standard project key with a pointed
    #    message before a network round-trip. (Unlike validate-key, submit
    #    is a state-changing admin action, so a 400 is the right shape.)
    if not _looks_like_admin_key(key):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "wrong_key_type",
                "message": _WRONG_KEY_TYPE_REASON,
            },
        )

    # 2. Probe — confirm the key actually reaches the OpenAI Admin API.
    #    A 403/401 on the admin probe means the key is unusable; 400 and
    #    do NOT seal. A 429 → 503.
    client = _openai_client_factory(key)
    try:
        result = _probe_openai_key(client)
    finally:
        client.close()

    if result.rate_limited is not None:
        raise _rate_limited(result.rate_limited)

    if result.hard_auth_failure or not result.admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_openai_key",
                "message": _INVALID_KEY_REASON,
            },
        )

    # 3. Seal (UPSERT — re-submitting rotates the key in place).
    try:
        seal_secret(
            tenant_id,
            OPENAI_ADMIN_KEY_SECRET,
            key.encode("utf-8"),
        )
    except LookupError as exc:
        # No DEK provisioned — a fully-onboarded tenant always has one
        # (the Anthropic select-region step provisions it). This is a
        # "shouldn't happen" provisioning gap, not user error.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "tenant_not_provisioned",
                "message": (
                    "Your tenant isn't fully provisioned yet. Finish "
                    "onboarding (region select) before connecting an "
                    "OpenAI Admin key."
                ),
            },
        ) from exc

    # 4. Enqueue the backfill pulls (usage / costs / projects). Best-effort
    #    after the seal: the key is sealed and the 15-minute beat will pick
    #    the streams up regardless, so a dispatch hiccup must not fail the
    #    request (which would leave the UI thinking the key didn't take).
    enqueued: list[str] = []
    try:
        enqueued = _backfill_dispatcher(tenant_id)
    except Exception:  # noqa: BLE001 — best-effort; seal already committed
        _log.exception(
            "openai-onboarding: backfill dispatch failed for tenant %s "
            "(key is sealed; the beat will pick up the streams)",
            tenant_id,
        )

    _log.info(
        "openai-onboarding: sealed openai_admin_key for tenant %s — "
        "enqueued %s",
        tenant_id,
        enqueued,
    )
    return OpenAISubmitResponse(
        sealed=True,
        region="us",
        org_id=result.org_id,
        capabilities=result.to_capabilities(),
        backfill_enqueued=enqueued,
    )
