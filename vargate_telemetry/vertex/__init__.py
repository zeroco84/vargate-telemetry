# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Google Vertex AI ingest for Ogma — client + types + factory (TM9 SCAFFOLD).

Ogma's THIRD vendor, after Anthropic (T3) and OpenAI (TM8). Per layout
decision A (CLAUDE.md "TM8 conventions"), this vendor package holds ONLY
the API I/O surface (clients / types / factory / exceptions / auth).
Celery pull tasks live in top-level ``tasks/pull_vertex_*.py`` and the
rate card in ``pricing/vertex_rates.py``.

SCAFFOLD scope (TM9, no live GCP project yet)
=============================================

This is the NON-GCP-dependent foundation: structure + skeletons grounded
in desk recon + the established multi-vendor pattern. Every
live-project-dependent specific (the BigQuery filter literal, the
Monitoring metric/label names, onboarding-supplied dataset config, the
exact google exception types) is marked ``# TODO(TM9 Phase A): …``. The
``google-*`` libraries are not installed yet — the modules import them at
top level (mirroring the OpenAI client's ``import httpx``) so the package
is ``py_compile``-clean before the deps land.

Auth (LOCKED — MVP): service-account JSON key sealed under
``GCP_SA_SECRET`` (``"gcp_service_account"``); credentials minted via
``credentials_for_tenant``. There is no ``tenant_credentials`` table and
no WIF (a planned fast-follow, seam left in ``auth.py``).

Streams (LOCKED — MVP, audit DEFERRED): cost via the BigQuery billing
export, usage via Cloud Monitoring ``token_count``.

Attribution (LOCKED): project / team ONLY — Google exposes no
per-user-email attribution, so there is no users side-table and the
cross-vendor email reconciler is intentionally untouched.
"""

from vargate_telemetry.vertex.auth import (
    GCP_SA_SECRET,
    GCP_SCOPES,
    credentials_for_tenant,
)
from vargate_telemetry.vertex.client import (
    TOKEN_COUNT_METRIC,
    VertexBillingClient,
    VertexMonitoringClient,
)
from vargate_telemetry.vertex.exceptions import (
    GCPAuthError,
    GCPError,
    PermissionDenied,
)
from vargate_telemetry.vertex.factory import gcp_clients_for_tenant
from vargate_telemetry.vertex.types import (
    BillingRow,
    Credit,
    Label,
    TokenUsagePoint,
)

__all__ = [
    "GCP_SA_SECRET",
    "GCP_SCOPES",
    "TOKEN_COUNT_METRIC",
    "BillingRow",
    "Credit",
    "GCPAuthError",
    "GCPError",
    "Label",
    "PermissionDenied",
    "TokenUsagePoint",
    "VertexBillingClient",
    "VertexMonitoringClient",
    "credentials_for_tenant",
    "gcp_clients_for_tenant",
]
