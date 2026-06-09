# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Exceptions raised by the Google Vertex AI clients (TM9 SCAFFOLD).

Mirrors ``openai/exceptions.py`` / ``anthropic/exceptions.py`` in spirit
so the parallel Vertex pull tasks can ``except GCPError`` /
``except PermissionDenied`` with the same control flow the other vendors
use. Google's surface differs in two ways the exception hierarchy
captures:

  - There is no rate-limit retry axis worth modeling here yet. The
    google-cloud client libraries (``google-cloud-bigquery`` /
    ``google-cloud-monitoring``) carry their own gRPC retry/backoff for
    transient ``ResourceExhausted`` (429-equivalent) and ``Unavailable``
    (503-equivalent) errors, so the scaffold does NOT add a tenacity
    wrapper on top — unlike the httpx-based OpenAI/Anthropic clients,
    which retry ``RateLimited`` themselves. A dedicated ``RateLimited``
    can be added later if the library defaults prove insufficient.
  - ``PermissionDenied`` (403-equivalent) is the per-stream soft-skip
    signal — the analogue of OpenAI ``InsufficientScope``. A service
    account can authenticate fine (so credential minting succeeds) yet
    lack ``bigquery.jobs.create`` on the billing-export project, or
    ``monitoring.timeSeries.list`` on the metrics project. Each stream's
    pull task catches it and returns a status dict
    (``no_billing_access`` / ``no_monitoring_access``) instead of
    failing the whole dispatch.

``GCPAuthError`` is the credential-minting failure: a malformed /
revoked service-account JSON blob, or a token-refresh that the Google
auth library rejects. Distinct from ``PermissionDenied`` because the
former means "we never got a usable token" (the whole tenant is
unusable) while the latter means "we have a token but this one API is
gated" (per-stream skip).

# TODO(TM9 Phase A): once the live GCP project exists, confirm which
# google-api-core / google-auth exception types actually surface for the
# 403 and auth-refresh cases (e.g. ``google.api_core.exceptions.
# PermissionDenied``, ``google.auth.exceptions.RefreshError``) and wrap
# them into these types at the client boundary so callers only ever see
# this module's hierarchy.
"""

from __future__ import annotations


class GCPError(Exception):
    """Base class for any Google Cloud / Vertex AI client failure.

    Carries a human-readable message; subclasses add structured context
    (HTTP-equivalent status, the GCP resource that was denied). A broad
    ``except GCPError`` catches every Vertex-vendor failure the same way
    ``except OpenAIAPIError`` catches the OpenAI family.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class GCPAuthError(GCPError):
    """Credential minting / token refresh failed — the tenant is unusable.

    Raised when the sealed service-account JSON cannot be turned into a
    live OAuth2 access token: a malformed/garbled blob, a revoked or
    disabled service account, or a refresh the Google auth library
    rejects. Distinct from :class:`PermissionDenied` — this means we
    never obtained a usable token at all, so there is nothing to
    per-stream-skip; the whole Vertex pull for the tenant aborts.

    # TODO(TM9 Phase A): map ``google.auth.exceptions.RefreshError`` (and
    # the malformed-key ``ValueError`` from
    # ``from_service_account_info``) onto this type in ``auth.py`` so the
    # raw google-auth exceptions never escape the vendor package.
    """


class PermissionDenied(GCPError):
    """403-equivalent — the service account lacks IAM on this surface.

    The per-stream soft-skip signal (analogue of OpenAI
    ``InsufficientScope``). The SA authenticated fine but is missing the
    role/permission a specific API needs:

      - BigQuery billing export read needs ``bigquery.jobs.create`` on
        the billing project **and** ``bigquery.tables.getData`` on the
        export dataset;
      - Cloud Monitoring read needs ``monitoring.timeSeries.list`` on
        the metrics-scope project.

    The pull task catches this and advances cleanly with a status of
    ``no_billing_access`` / ``no_monitoring_access`` rather than
    failing the dispatch for every other tenant.

    ``resource`` (optional) records which GCP resource was denied for
    logging. ``status_code`` defaults to 403 for symmetry with the
    other vendors' typed 403s.

    # TODO(TM9 Phase A): confirm the live google-api-core type is
    # ``google.api_core.exceptions.PermissionDenied`` (gRPC) vs
    # ``Forbidden`` (REST) for each library and translate at the client
    # boundary so this is the only 403 type callers handle.
    """

    def __init__(
        self, message: str, *, resource: str | None = None
    ) -> None:
        super().__init__(message)
        self.resource = resource
        self.status_code = 403
