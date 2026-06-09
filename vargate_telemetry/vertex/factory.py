# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Per-tenant Google Vertex AI client factory (TM9 SCAFFOLD).

Single entry point — :func:`gcp_clients_for_tenant` — that mints the
tenant's Google credentials (via :func:`vargate_telemetry.vertex.auth.
credentials_for_tenant`) and wires BOTH read clients (billing +
monitoring). The Vertex analogue of ``openai/factory.py``'s
``admin_client_for_tenant`` — same ``LookupError``-is-the-no-creds-
soft-skip error contract — but it returns a *pair* of clients plus a
small ``meta`` dict, because Google's billing and monitoring surfaces are
separate clients (and the billing query needs onboarding-supplied dataset
config the monitoring read doesn't).

Returns ``(billing_client, monitoring_client, meta)`` where ``meta``
carries the onboarding-resolved GCP config the pull tasks need to drive
the clients:

  - ``project`` — the GCP project id the clients are bound to.
  - ``billing_dataset`` — the BigQuery dataset holding the export table.
  - ``billing_account`` — the billing-account id suffix on the export
    table name (``gcp_billing_export_v1_<ACCT>``).
  - ``billing_location`` — the dataset's BigQuery region (the cost query
    job must run there).

# TODO(TM9 Phase A): the ``meta`` values are tenant-specific onboarding
# output that does NOT exist yet (no live GCP project). Resolve them from
# wherever the Vertex onboarding flow persists them — most likely a small
# sealed JSON config blob alongside the SA key (e.g. a
# ``"gcp_vertex_config"`` secret) OR dedicated columns. The placeholders
# below let the scaffold compile and make the seam obvious; wire the real
# source in Phase A.
"""

from __future__ import annotations

from typing import Any, Tuple

from vargate_telemetry.vertex.auth import credentials_for_tenant
from vargate_telemetry.vertex.client import (
    VertexBillingClient,
    VertexMonitoringClient,
)


def _resolve_gcp_meta(tenant_id: str, credentials: Any) -> dict[str, Any]:
    """Resolve the onboarding-supplied GCP config for the tenant.

    The minted ``credentials`` already know the SA's home ``project_id``
    (``credentials.project_id`` on a service-account credential), which
    is the natural default for the project the clients bind to. The
    billing-export dataset / account / location, however, are
    onboarding-time choices the SA JSON does NOT carry.

    # TODO(TM9 Phase A): load ``billing_dataset`` / ``billing_account`` /
    # ``billing_location`` from the real onboarding store (a sealed
    # ``gcp_vertex_config`` blob via ``unseal_secret``, or dedicated
    # columns). Until that flow exists, fall back to the SA's project_id
    # for ``project`` and leave the billing fields as explicit
    # ``None`` placeholders so a pull task that needs them fails loudly
    # rather than querying a wrong table.
    """
    project = getattr(credentials, "project_id", None)
    return {
        "project": project,
        # TODO(TM9 Phase A): real onboarding values (see module docstring).
        "billing_dataset": None,
        "billing_account": None,
        "billing_location": None,
    }


def gcp_clients_for_tenant(
    tenant_id: str,
) -> Tuple[VertexBillingClient, VertexMonitoringClient, dict[str, Any]]:
    """Build the tenant's Vertex billing + monitoring clients.

    Mints the tenant's Google credentials from the sealed
    ``gcp_service_account`` JSON, resolves the onboarding GCP config, and
    returns ``(billing_client, monitoring_client, meta)``. The caller
    owns both clients' lifetimes (close them, or use ``with`` blocks).

    Raises:
      - ``LookupError`` (propagated from ``credentials_for_tenant`` →
        ``unseal_secret``) when the tenant has no DEK provisioned or no
        ``gcp_service_account`` sealed — the **no-creds soft-skip
        signal**; the pull tasks return ``status="no_gcp_creds"`` (no
        retry).
      - :class:`vargate_telemetry.vertex.exceptions.GCPAuthError` when the
        sealed blob exists but can't be minted/refreshed.

    The minted credentials (and the SA private key inside them) live only
    on the returned clients; callers MUST NOT persist or log them.
    """
    if not tenant_id:
        raise ValueError("tenant_id required")

    credentials = credentials_for_tenant(tenant_id)
    meta = _resolve_gcp_meta(tenant_id, credentials)

    project = meta.get("project")
    if not project:
        # The SA JSON always carries project_id; an empty one means a
        # malformed/unexpected key shape. Treat as an auth-config gap
        # rather than silently binding a client to no project.
        #
        # # TODO(TM9 Phase A): once onboarding supplies an explicit
        # # project override, prefer it here and only fall back to the
        # # SA's project_id.
        raise ValueError(
            f"could not resolve a GCP project for tenant {tenant_id!r} "
            "(service-account key has no project_id and onboarding "
            "supplied no override)"
        )

    billing_client = VertexBillingClient(credentials, project)
    monitoring_client = VertexMonitoringClient(credentials, project)
    return billing_client, monitoring_client, meta
