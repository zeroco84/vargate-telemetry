# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Google Vertex AI onboarding (TM9 scaffold).

Adds **Google Cloud credentials** (a service-account JSON key + the
BigQuery billing-export config) to an already-provisioned tenant,
alongside the Anthropic + OpenAI credentials. Google is Ogma's THIRD
vendor — this is the third-vendor card on the onboarding/Settings →
Integrations grid, NOT a tenant-creating flow. The tenant already has a
DEK (from the Anthropic ``select-region`` step); we seal the Google
secrets under that same DEK.

Two routes, mirroring ``openai_onboarding.py``'s validate-then-seal
separation, namespaced under ``/onboarding/google/`` so the frontend
mirrors them cleanly:

  1. ``POST /onboarding/google/validate`` — the **parse + checklist**
     step (member-runnable, ``current_user``). Parses the supplied SA
     JSON and validates it carries ``client_email`` / ``private_key`` /
     ``token_uri``, plus the billing-export dataset name + location +
     project id. Returns a capability-checklist skeleton. A malformed
     key / missing field returns ``valid: false`` with a human
     ``reason`` — **never a 500**.

     LOCKED: in the scaffold this does NOT make a live GCP call — the
     live probe (mint the credential, run a 1-row billing query, list one
     Monitoring series) is ``# TODO(TM9 Phase A)``. The checklist is
     reported from the static parse only; every probe bool is False with
     a Phase-A TODO.

  2. ``POST /onboarding/google/submit`` — the **seal + backfill** step
     (admin-gated, ``require_admin``). Re-validates, seals the SA JSON
     under ``GCP_SA_SECRET`` (``"gcp_service_account"``) and the
     billing-export config under ``GCP_BILLING_CONFIG_SECRET`` (both
     UPSERT, so re-submitting rotates), then enqueues the backfill
     (the per-tenant costs + usage pulls). OpenAI/Google have no EU
     residency in this MVP, so the region is always ``"us"``.

Attribution (LOCKED): project/team only — there is NO per-user
capability in the checklist, NO users side-table, and the email
reconciler is untouched. ``audit`` is structurally absent (DEFERRED).

Build posture: scaffold. The live close-out (Phase A) is a real SA key +
billing dataset submitted here → the live probe flips the checklist →
the Vertex pulls start landing rows. Every live-GCP specific carries a
``# TODO(TM9 Phase A): ...`` marker.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from vargate_telemetry.auth.middleware import AuthenticatedUser, current_user
from vargate_telemetry.auth.roles import require_admin
from vargate_telemetry.crypto.seal import seal_secret

# NOTE: ``GCP_SA_SECRET`` is imported lazily inside ``submit`` (not at
# module top) on purpose. ``vargate_telemetry.vertex.auth`` does a
# top-level ``import google.auth`` (mirroring the OpenAI client's
# top-level ``import httpx``), and the google libs are NOT installed until
# the Integrate phase adds them. A module-level import here would make
# ``api/app.py`` fail to boot the moment it includes this router. The
# lazy import keeps the router importable pre-deps while still sourcing
# the secret name from its single definition in ``vertex.auth``.

_log = logging.getLogger(__name__)

router = APIRouter()


# Secret name for the per-tenant billing-export config (dataset +
# location + project id), sealed alongside the SA key under the same DEK.
# Distinct from GCP_SA_SECRET. The factory's _resolve_gcp_meta reads this
# in Phase A. NOTE: the name MUST match the secret the factory unseals —
# vertex/factory._resolve_gcp_meta's Phase-A TODO references this exact
# "gcp_vertex_config" name, so the producer (here) and consumer (factory)
# agree on one contract.
# TODO(TM9 Phase A): wire vertex/factory._resolve_gcp_meta to unseal +
# parse this secret into {project, billing_dataset, billing_account,
# billing_location} (it currently returns None placeholders). The seal
# here + that read are the two ends of the same contract.
GCP_BILLING_CONFIG_SECRET = "gcp_vertex_config"

# Region is always "us" in the MVP (no EU residency), echoed for symmetry
# with the Anthropic/OpenAI flows.
DEFAULT_REGION = "us"


# ───────────────────────────────────────────────────────────────────────────
# Request / response shapes
# ───────────────────────────────────────────────────────────────────────────
#
# TODO(TM9 Phase A): add the matching schemas to openapi/ogma-api.yaml
# (GoogleValidateRequest / GoogleKeyCapabilities / GoogleValidateResponse
# / GoogleSubmitResponse) + bump the frontend codegen ref, same as the
# OpenAI onboarding shapes.


class GoogleOnboardingRequest(BaseModel):
    """The shared validate/submit request body.

    Carries the service-account JSON key (as a JSON string OR an already-
    parsed object) plus the billing-export config the cost pull needs.
    """

    service_account_json: str = Field(
        ...,
        min_length=2,
        description=(
            "The Google service-account key, as the raw JSON string from "
            "the GCP console ('Create key → JSON'). Must contain "
            "`client_email`, `private_key`, and `token_uri`."
        ),
    )
    billing_dataset: str = Field(
        ...,
        min_length=1,
        description=(
            "The BigQuery dataset holding the Cloud Billing export "
            "(the `gcp_billing_export_v1_*` table lives here)."
        ),
    )
    billing_location: str = Field(
        ...,
        min_length=1,
        description=(
            "The BigQuery dataset's location/region (e.g. `US`, "
            "`europe-west1`). BigQuery query jobs must run in this region."
        ),
    )
    project_id: str = Field(
        ...,
        min_length=1,
        description=(
            "The GCP project id that runs/bills the BigQuery query jobs "
            "and is the monitored project for token-usage time series."
        ),
    )
    billing_account: str = Field(
        ...,
        min_length=1,
        description=(
            "The billing-account id suffix on the export table name "
            "(`gcp_billing_export_v1_<ACCT>`), with `-` already replaced "
            "by `_`. The cost query resolves the concrete table from this "
            "+ `billing_dataset`."
        ),
    )


class GoogleKeyCapabilities(BaseModel):
    """Per-credential capability checklist — field names mirror the
    ``google`` block of ``GET /me/capabilities`` so one UI matrix renders
    both.

    LOCKED shape (project/team attribution only):
      - ``cost``              — the billing export is readable (a 1-row
                                BigQuery probe returned). Live probe is
                                Phase A; the scaffold reports False.
      - ``usage``             — Cloud Monitoring token_count is readable
                                (a 1-series probe returned). Phase A.
      - ``project_attribution`` — always True (Google always attributes
                                to a project).
      - ``team_labels``       — a non-empty request label was seen on a
                                billing row (the team dim). Phase A.
      - ``per_user_breakdown`` — always **False** (STRUCTURAL — Google has
                                no per-user-email attribution).
      - ``audit``             — always **False** (DEFERRED in TM9).
    """

    cost: bool
    usage: bool
    project_attribution: bool
    team_labels: bool
    per_user_breakdown: bool
    audit: bool


def _scaffold_capabilities() -> GoogleKeyCapabilities:
    """The static checklist the scaffold reports (no live GCP probe).

    ``project_attribution`` is True (structural — Google always has a
    project). ``per_user_breakdown`` / ``audit`` are False (structural /
    deferred). ``cost`` / ``usage`` / ``team_labels`` are False until the
    Phase-A live probe fills them.
    TODO(TM9 Phase A): replace the False placeholders for cost / usage /
    team_labels with the live probe results (mint creds via
    vertex.credentials_for_tenant; run a 1-row VertexBillingClient query +
    a 1-series VertexMonitoringClient read; team_labels ← any non-empty
    label on the probed billing row).
    """
    return GoogleKeyCapabilities(
        cost=False,
        usage=False,
        project_attribution=True,
        team_labels=False,
        per_user_breakdown=False,  # STRUCTURAL — never True for Google
        audit=False,  # DEFERRED in TM9
    )


class GoogleValidateResponse(BaseModel):
    valid: bool = Field(
        ...,
        description=(
            "True iff the service-account JSON parsed and carries the "
            "required fields (`client_email`/`private_key`/`token_uri`) "
            "and the billing config is present. False — with a `reason` "
            "— on a malformed key or missing field; never 500s. NOTE "
            "(TM9 scaffold): this is a STATIC parse — it does not yet "
            "confirm the credential works against live GCP (Phase A)."
        ),
    )
    client_email: Optional[str] = Field(
        None,
        description=(
            "The service account's `client_email`, echoed back as a "
            "confirmation. Null when the key didn't parse."
        ),
    )
    reason: Optional[str] = Field(
        None,
        description="Human-readable explanation when `valid` is false.",
    )
    capabilities: GoogleKeyCapabilities


class GoogleSubmitResponse(BaseModel):
    sealed: bool = Field(
        ...,
        description=(
            "True once the SA key + billing config are validated + sealed "
            "for the tenant."
        ),
    )
    region: str = Field(
        ...,
        description="Always `us` (no EU residency in the MVP).",
    )
    client_email: Optional[str] = Field(
        None,
        description="The sealed service account's `client_email`.",
    )
    capabilities: GoogleKeyCapabilities
    backfill_enqueued: list[str] = Field(
        default_factory=list,
        description=(
            "The Vertex ingest streams whose backfill pulls were enqueued "
            "on submit, e.g. `['vertex_billing_costs', "
            "'vertex_token_usage']`. The 15-minute beat picks the streams "
            "up thereafter; these one-shot dispatches seed the dashboard."
        ),
    )


# ───────────────────────────────────────────────────────────────────────────
# Backfill-dispatch injection point (mirrors openai_onboarding's
# set_backfill_dispatcher_for_test). Production fans out the per-tenant
# pull tasks via ``.delay``; tests substitute a recorder.
# ───────────────────────────────────────────────────────────────────────────


BackfillDispatcher = Callable[[str], list[str]]
"""(tenant_id) -> the list of stream names that were enqueued."""


# The streams seeded on submit. ``audit`` is intentionally absent
# (DEFERRED in TM9). costs + usage are what light up the dashboard for
# the just-onboarded tenant.
BACKFILL_STREAMS = [
    "vertex_billing_costs",
    "vertex_token_usage",
]


def _default_backfill_dispatcher(tenant_id: str) -> list[str]:
    """Enqueue the one-shot backfill pulls for freshly-sealed Google creds.

    Dispatches the per-tenant costs + usage pull tasks (each a
    cursor-driven lookback that, on first run with no cursor, pulls the
    initial window and advances). Imported lazily so importing this route
    module doesn't pull the whole Celery task graph at app-import time
    (matches the openai_onboarding.py late-import posture).
    """
    from vargate_telemetry.tasks.pull_vertex_costs import (
        pull_vertex_costs_for_tenant,
    )
    from vargate_telemetry.tasks.pull_vertex_usage import (
        pull_vertex_usage_for_tenant,
    )

    pull_vertex_costs_for_tenant.delay(tenant_id)
    pull_vertex_usage_for_tenant.delay(tenant_id)
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
    "That doesn't look like a valid Google service-account key. Create one "
    "at console.cloud.google.com → IAM & Admin → Service Accounts → Keys → "
    "Add key → JSON, and paste the whole JSON file. It must contain "
    "`client_email`, `private_key`, and `token_uri`."
)

# The SA JSON fields a usable key must carry (mirrors vertex.auth's
# _load_sa_info check so validate + the eventual mint agree).
_REQUIRED_SA_FIELDS = ("client_email", "private_key", "token_uri")


# ───────────────────────────────────────────────────────────────────────────
# Shared parse — by validate + submit.
# ───────────────────────────────────────────────────────────────────────────


class _ParseResult:
    """Carrier for a static SA-JSON parse: the ``ok`` flag, the parsed
    ``client_email`` (when ok), and a human ``reason`` on failure."""

    def __init__(self) -> None:
        self.ok = False
        self.client_email: Optional[str] = None
        self.reason: Optional[str] = None


def _parse_sa_json(raw: str) -> _ParseResult:
    """Statically parse + field-check the service-account JSON.

    Returns a :class:`_ParseResult`. Never raises for bad input — a
    malformed key / missing field is a normal UI state (``valid: false``),
    not a server error. This mirrors ``vertex.auth._load_sa_info``'s field
    check WITHOUT minting a credential (that's the Phase-A live probe).
    """
    result = _ParseResult()
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        result.reason = _INVALID_KEY_REASON
        return result
    if not isinstance(info, dict):
        result.reason = _INVALID_KEY_REASON
        return result
    missing = [f for f in _REQUIRED_SA_FIELDS if not info.get(f)]
    if missing:
        result.reason = _INVALID_KEY_REASON
        return result
    result.ok = True
    result.client_email = info.get("client_email")
    return result


def _billing_config_json(body: GoogleOnboardingRequest) -> str:
    """Canonical JSON for the sealed billing-export config blob.

    The factory's ``_resolve_gcp_meta`` (Phase A) unseals + parses this
    into the ``meta`` dict it returns. The KEY NAMES here MUST match the
    keys ``_resolve_gcp_meta`` returns + the pull tasks read —
    ``project`` / ``billing_dataset`` / ``billing_account`` /
    ``billing_location`` — so the producer (this seal) and consumer
    (factory) agree on one contract. ``project`` here is the explicit
    onboarding override; the factory currently falls back to the SA key's
    ``project_id`` and Phase A prefers this sealed value.
    """
    return json.dumps(
        {
            "project": body.project_id,
            "billing_dataset": body.billing_dataset,
            "billing_account": body.billing_account,
            "billing_location": body.billing_location,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


# ───────────────────────────────────────────────────────────────────────────
# POST /onboarding/google/validate
# ───────────────────────────────────────────────────────────────────────────


@router.post(
    "/onboarding/google/validate",
    response_model=GoogleValidateResponse,
    operation_id="validateGoogleCredentials",
    tags=["onboarding"],
    summary="Validate a Google service-account key + config (parse only)",
)
def validate_google_credentials(
    body: GoogleOnboardingRequest,
    user: AuthenticatedUser = Depends(current_user),
) -> GoogleValidateResponse:
    """Statically validate the supplied Google credentials + billing config.

    Member-runnable (``current_user``) — no tenant side effects, the admin
    gate lands on submit. A malformed key / missing field returns
    ``valid: false`` with a ``reason``, never a 500.

    LOCKED (TM9 scaffold): this does NOT make a live GCP call. The
    capability checklist is the static skeleton (``project_attribution``
    True; ``per_user_breakdown`` / ``audit`` False structurally; ``cost``
    / ``usage`` / ``team_labels`` False until the Phase-A live probe).
    TODO(TM9 Phase A): after the static parse passes, mint a credential
    via ``vertex.credentials_for_tenant`` (or directly from the posted
    JSON), run a 1-row ``VertexBillingClient.query_costs`` + a 1-series
    ``VertexMonitoringClient.list_token_usage`` probe, and fill
    cost/usage/team_labels from the results.
    """
    parse = _parse_sa_json(body.service_account_json)
    if not parse.ok:
        return GoogleValidateResponse(
            valid=False,
            client_email=None,
            reason=parse.reason,
            capabilities=_scaffold_capabilities(),
        )

    return GoogleValidateResponse(
        valid=True,
        client_email=parse.client_email,
        reason=None,
        capabilities=_scaffold_capabilities(),
    )


# ───────────────────────────────────────────────────────────────────────────
# POST /onboarding/google/submit
# ───────────────────────────────────────────────────────────────────────────


@router.post(
    "/onboarding/google/submit",
    response_model=GoogleSubmitResponse,
    operation_id="submitGoogleCredentials",
    tags=["onboarding"],
    summary="Validate + seal Google credentials and enqueue the backfill",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": (
                "The service-account JSON is malformed or missing required "
                "fields (`code: invalid_gcp_key`)."
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
    },
)
def submit_google_credentials(
    body: GoogleOnboardingRequest,
    user: AuthenticatedUser = Depends(require_admin),
) -> GoogleSubmitResponse:
    """Validate, seal the SA key + billing config, then enqueue the backfill.

    ``require_admin`` guarantees a bound tenant + admin role. The secrets
    are only sealed after the static parse passes — a bad/wrong key 400s
    without touching ``encrypted_secrets``, so there's no partial state.
    Re-submitting rotates the sealed secrets in place (UPSERT) and
    re-enqueues the backfill (each pull task is idempotent via the
    ``(tenant, source_api, external_id)`` dedup).

    Region is always ``"us"`` (no EU residency in the MVP).

    TODO(TM9 Phase A): before sealing, run the live GCP probe (as in
    validate) and 400 if the credential can't authenticate — the scaffold
    seals on the static parse alone, so a syntactically-valid-but-dead key
    would seal and then soft-skip in the pulls. That's acceptable for the
    scaffold (no live project yet) but Phase A should reject a dead key
    here.
    """
    tenant_id = user.tenant_id  # require_admin guarantees non-None
    assert tenant_id is not None  # for type-checkers; require_admin enforces

    # 1. Static parse — reject a malformed key before touching secrets.
    parse = _parse_sa_json(body.service_account_json)
    if not parse.ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_gcp_key",
                "message": parse.reason or _INVALID_KEY_REASON,
            },
        )

    # 2. Seal the SA JSON + the billing config (both UPSERT — re-submitting
    #    rotates in place). Seal the SA JSON exactly as posted so the mint
    #    path (vertex.auth.credentials_for_tenant) re-parses the identical
    #    bytes. Lazy import (see the module-top NOTE): vertex.auth pulls in
    #    google-auth at import, absent until the Integrate phase.
    from vargate_telemetry.vertex.auth import GCP_SA_SECRET

    try:
        seal_secret(
            tenant_id,
            GCP_SA_SECRET,
            body.service_account_json.encode("utf-8"),
        )
        seal_secret(
            tenant_id,
            GCP_BILLING_CONFIG_SECRET,
            _billing_config_json(body).encode("utf-8"),
        )
    except LookupError as exc:
        # No DEK provisioned — a fully-onboarded tenant always has one
        # (the Anthropic select-region step provisions it). A "shouldn't
        # happen" provisioning gap, not user error.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "tenant_not_provisioned",
                "message": (
                    "Your tenant isn't fully provisioned yet. Finish "
                    "onboarding (region select) before connecting Google "
                    "credentials."
                ),
            },
        ) from exc

    # 3. Enqueue the backfill pulls (costs / usage). Best-effort after the
    #    seal: the secrets are sealed and the 15-minute beat will pick the
    #    streams up regardless, so a dispatch hiccup must not fail the
    #    request (which would leave the UI thinking the key didn't take).
    enqueued: list[str] = []
    try:
        enqueued = _backfill_dispatcher(tenant_id)
    except Exception:  # noqa: BLE001 — best-effort; seal already committed
        _log.exception(
            "google-onboarding: backfill dispatch failed for tenant %s "
            "(secrets are sealed; the beat will pick up the streams)",
            tenant_id,
        )

    _log.info(
        "google-onboarding: sealed gcp_service_account + gcp_billing_config "
        "for tenant %s — enqueued %s",
        tenant_id,
        enqueued,
    )
    return GoogleSubmitResponse(
        sealed=True,
        region=DEFAULT_REGION,
        client_email=parse.client_email,
        capabilities=_scaffold_capabilities(),
        backfill_enqueued=enqueued,
    )
