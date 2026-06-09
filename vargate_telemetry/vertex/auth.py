# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Per-tenant Google Cloud credential minting (TM9 SCAFFOLD).

Turns the sealed **service-account JSON key** in ``encrypted_secrets``
into a live, refreshed ``google.auth`` credentials object the Vertex
BigQuery / Cloud Monitoring clients consume. This is the Vertex analogue
of ``openai/factory.py``'s ``admin_client_for_tenant`` /
``anthropic/factory.py`` — same per-tenant-DEK ``unseal_secret`` read
path, same ``LookupError``-is-a-soft-skip error contract — but the
unsealed bytes are a JSON blob, not a bearer string, and they're minted
into OAuth2 credentials rather than dropped onto an HTTP header.

Auth model (LOCKED — MVP)
=========================

The MVP authenticates with a **service-account JSON key** sealed under
the secret name ``"gcp_service_account"`` (see :data:`GCP_SA_SECRET`).
There is **no ``tenant_credentials`` table** — the SA JSON lives in the
same ``encrypted_secrets`` store as the Anthropic/OpenAI keys, under the
tenant's DEK, so a tenant can hold all three vendors' credentials at
once. The blob is the full JSON Google emits on key creation
(``{"type": "service_account", "project_id": ..., "private_key": ...,
"client_email": ..., ...}``).

Minting walks the documented google-auth path:

    info = json.loads(unseal_secret(tenant, GCP_SA_SECRET))
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=GCP_SCOPES
    )
    creds.refresh(google.auth.transport.requests.Request())

The refresh exchanges the SA's signed JWT for a short-lived OAuth2
access token up front so the first BigQuery/Monitoring call doesn't pay
the refresh latency and so a bad key fails *here* (as ``GCPAuthError``)
rather than mid-pull. The client libraries refresh again on expiry on
their own.

Scopes (LOCKED): read-only on both surfaces —
``https://www.googleapis.com/auth/bigquery.readonly`` (billing export)
and ``https://www.googleapis.com/auth/monitoring.read`` (token_count).

WIF seam (NOT built)
====================

Workload Identity Federation is a planned fast-follow (keyless auth via
an external-account credential). The seam is the single
:func:`credentials_for_tenant` chokepoint: a later change inspects the
sealed blob's ``"type"`` (``"external_account"`` → WIF via
``google.auth.identity_pool`` / ``aws``) vs ``"service_account"`` and
mints accordingly, with the rest of the package unchanged. DO NOT build
WIF here — MVP is SA-JSON only.

Error contract
==============

  - ``LookupError`` (propagated from ``unseal_secret``) when the tenant
    has no DEK provisioned **or** no ``gcp_service_account`` sealed —
    the **no-creds soft-skip signal**. The pull tasks catch it and
    return ``status="no_gcp_creds"`` (no retry), exactly like the
    OpenAI ``no_openai_key`` path.
  - ``GCPAuthError`` when the blob exists but can't be minted/refreshed
    (malformed JSON, revoked key, refresh rejected) — a genuine
    credential failure, distinct from the soft-skip.
"""

from __future__ import annotations

import json
from typing import Any

# NOTE: google-auth is NOT yet in requirements.txt — the Integrate phase
# adds ``google-auth`` / ``google-cloud-bigquery`` / ``google-cloud-
# monitoring``. Importing at module top mirrors the OpenAI client's
# top-level ``import httpx`` / ``tenacity`` posture; ``py_compile`` checks
# syntax only, so the scaffold compiles before the deps land.
import google.auth.transport.requests
from google.oauth2 import service_account

from vargate_telemetry.crypto.seal import unseal_secret
from vargate_telemetry.vertex.exceptions import GCPAuthError

# Stable secret name for the tenant's Google **service-account JSON
# key**. Distinct from ``anthropic_admin_key`` / ``openai_admin_key`` so
# all three vendors' credentials coexist under one tenant DEK. The sealed
# value is the full SA JSON blob Google emits on key creation.
GCP_SA_SECRET = "gcp_service_account"

# Read-only OAuth2 scopes (LOCKED). BigQuery for the billing export
# table; Cloud Monitoring for the publisher token_count metric.
#
# # TODO(TM9 Phase A): confirm these two scopes are sufficient against a
# # live project — in particular that ``bigquery.readonly`` covers
# # running a query *job* over the export dataset (vs needing
# # ``bigquery`` write-ish job scope) and that ``monitoring.read`` covers
# # ``projects.timeSeries.list``.
GCP_SCOPES = (
    "https://www.googleapis.com/auth/bigquery.readonly",
    "https://www.googleapis.com/auth/monitoring.read",
)


def credentials_for_tenant(tenant_id: str) -> Any:
    """Mint refreshed Google OAuth2 credentials for the tenant.

    Unseals the ``gcp_service_account`` JSON blob (under the tenant DEK),
    builds a ``service_account.Credentials`` with the read-only
    :data:`GCP_SCOPES`, and refreshes it once so the access token is live
    before the first API call.

    Returns the ``google.auth.credentials.Credentials`` object (typed
    ``Any`` here so the module compiles before ``google-auth`` is
    installed). Callers pass it straight to ``bigquery.Client(...,
    credentials=creds)`` / ``MetricServiceClient(credentials=creds)`` —
    see :mod:`vargate_telemetry.vertex.factory`.

    Raises:
      - ``LookupError`` (from ``unseal_secret``) when the tenant has no
        DEK provisioned or no ``gcp_service_account`` sealed. This is the
        **no-creds soft-skip signal** — the pull tasks treat it as
        ``status="no_gcp_creds"`` (no retry), NOT an error.
      - :class:`GCPAuthError` when the blob exists but can't be minted or
        refreshed (malformed JSON, revoked/disabled key, refresh
        rejected). A real credential failure.

    The plaintext SA key (private key material) lives only in the
    in-memory ``info`` dict and inside the returned credentials object;
    callers MUST NOT persist or log it.
    """
    if not tenant_id:
        raise ValueError("tenant_id required")

    # LookupError (no DEK / no sealed key) propagates as the soft-skip
    # signal — same contract as the OpenAI/Anthropic factories.
    raw = unseal_secret(tenant_id, GCP_SA_SECRET)

    try:
        info = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        # A sealed-but-garbled blob is an auth failure, not a missing
        # key — it should not look like the no-creds soft-skip.
        raise GCPAuthError(
            f"sealed gcp_service_account for tenant {tenant_id!r} is not "
            "valid JSON"
        ) from exc

    # TODO(TM9 Phase A): WIF seam — when WIF ships, branch on
    # ``info.get("type")``: ``"external_account"`` mints a WIF credential
    # (``google.auth.load_credentials_from_dict`` /
    # ``identity_pool.Credentials``), ``"service_account"`` keeps this
    # path. MVP is service-account only; reject anything else loudly.
    try:
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=list(GCP_SCOPES)
        )
    except (ValueError, KeyError) as exc:
        # from_service_account_info raises ValueError on a malformed key
        # (missing private_key / client_email, bad PEM).
        raise GCPAuthError(
            f"could not build service-account credentials for tenant "
            f"{tenant_id!r}: {exc}"
        ) from exc

    # Refresh up front so a bad/revoked key fails here (GCPAuthError)
    # rather than mid-pull, and the first real call skips the latency.
    #
    # # TODO(TM9 Phase A): confirm the live refresh-failure exception is
    # # ``google.auth.exceptions.RefreshError`` and catch it specifically
    # # here (the broad ``Exception`` below is the scaffold-safe floor).
    try:
        creds.refresh(google.auth.transport.requests.Request())
    except Exception as exc:  # noqa: BLE001 — narrowed in Phase A
        raise GCPAuthError(
            f"could not refresh service-account token for tenant "
            f"{tenant_id!r}: {exc}"
        ) from exc

    return creds
