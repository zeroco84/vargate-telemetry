# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Google Vertex AI read clients (TM9 SCAFFOLD).

Two thin clients over the google-cloud libraries, the Vertex analogue of
``openai/client.py``'s single ``OpenAIAdminClient`` (Google's two
surfaces don't share a transport, so this is two classes instead of one):

  - :class:`VertexBillingClient` — wraps ``google-cloud-bigquery``.
    ``query_costs(dataset, since, until)`` runs ONE parameterized query
    over the Cloud Billing export table
    (``gcp_billing_export_v1_<ACCT>``), grouping spend by day / sku /
    project / request-label set, filtered to Vertex by
    ``service.description``, and returns ``list[BillingRow]`` with the
    ``credits`` array carried through for the pull task to net.
  - :class:`VertexMonitoringClient` — wraps ``google-cloud-monitoring``.
    ``list_token_usage(since, until)`` calls
    ``projects.timeSeries.list`` for the ``token_count`` publisher metric
    and returns ``list[TokenUsagePoint]`` (one per series × point).

Both take pre-minted ``credentials`` (from
:func:`vargate_telemetry.vertex.auth.credentials_for_tenant`) plus the
GCP ``project`` and build their underlying client in ``__init__``. Both
translate a Google 403 into :class:`PermissionDenied` so the pull tasks
soft-skip per-stream, exactly as the OpenAI client raises
``InsufficientScope`` on 403.

Retry posture: unlike the httpx-based OpenAI/Anthropic clients (which
own a tenacity wrapper), the google-cloud libraries carry their own
transient-error retry, so this scaffold leans on the library default and
does NOT add a second layer.

# TODO(TM9 Phase A): every Google-API-shaped specific below — the exact
# SQL filter literal, the projected column aliases, the Monitoring metric
# filter string, the exception types for 403 — is desk-recon-grounded but
# UNCONFIRMED against a live project. Each is flagged inline.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# NOTE: the google-cloud libraries are NOT yet in requirements.txt — the
# Integrate phase adds ``google-cloud-bigquery`` / ``google-cloud-
# monitoring``. Top-level imports mirror the OpenAI client's top-level
# ``import httpx``; ``py_compile`` checks syntax only, so the scaffold
# compiles before the deps land.
from google.api_core.exceptions import (
    Forbidden,
    PermissionDenied as _GPermissionDenied,
)
from google.cloud import bigquery, monitoring_v3

from vargate_telemetry.vertex.exceptions import PermissionDenied
from vargate_telemetry.vertex.types import BillingRow, TokenUsagePoint


# Recon: the publisher token-count metric. ``type`` label splits
# input/output. Read via projects.timeSeries.list (Monitoring v3).
#
# # TODO(TM9 Phase A): confirm this is the exact, current metric type
# # against a live project (the publisher metric set has churned; the
# # "Gemini Enterprise Agent Platform" rebrand may have renamed it).
TOKEN_COUNT_METRIC = (
    "aiplatform.googleapis.com/publisher/online_serving/token_count"
)


class VertexBillingClient:
    """BigQuery reader for the Cloud Billing export (authoritative spend).

    Holds a ``bigquery.Client`` bound to ``project`` with the tenant's
    minted ``credentials``. The caller owns the lifetime; use ``close()``
    or a ``with`` block to release the underlying client.
    """

    def __init__(self, credentials: Any, project: str) -> None:
        if not project:
            raise ValueError("project required")
        self._project = project
        self._client = bigquery.Client(
            project=project, credentials=credentials
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "VertexBillingClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def query_costs(
        self,
        dataset: str,
        since: datetime,
        until: datetime,
        *,
        billing_account: str | None = None,
    ) -> list[BillingRow]:
        """Run the billing-export cost query for ``[since, until)``.

        Groups Vertex spend by usage day / sku / project / request-label
        set over the export table ``<project>.<dataset>.
        gcp_billing_export_v1_<ACCT>``, filtered to Vertex by
        ``service.description``, and returns one :class:`BillingRow` per
        group. ``credits`` rides through unnetted — the pull task
        computes ``net = cost + SUM(credits.amount)`` so the raw gross +
        credit deltas both stay inspectable.

        ``dataset`` (and the account suffix) come from onboarding — they
        are tenant-specific and NOT knowable at code-write time, hence
        the parameterized/format seam below.

        Parameters bind ``since`` / ``until`` as query parameters (NOT
        string-interpolated) so the time window can't inject SQL; the
        table identifier is validated/escaped separately because BigQuery
        can't parameterize a table name.

        Raises :class:`PermissionDenied` when the SA lacks BigQuery read
        IAM on the export dataset (per-stream soft-skip signal).

        # TODO(TM9 Phase A): everything about the SQL below is
        # desk-recon-grounded and MUST be confirmed against a live export:
        #   - the exact ``service.description`` literal that selects
        #     Vertex (post "Gemini Enterprise Agent Platform" rebrand);
        #   - the real table-name suffix
        #     (``gcp_billing_export_v1_<BILLING_ACCT_ID>``) — the account
        #     id comes from onboarding;
        #   - the dataset LOCATION (the BigQuery job must run in the
        #     dataset's region) — also from onboarding;
        #   - the label-array UNNEST + the projected column aliases line
        #     up with ``BillingRow`` field names.
        """
        if not dataset:
            raise ValueError("dataset required")

        # BigQuery can't parameterize a table identifier, so it's built
        # by format — every interpolated piece is an identifier we
        # validate, never user free-text. The time window IS
        # parameterized below.
        #
        # # TODO(TM9 Phase A): validate ``dataset`` / ``billing_account``
        # # as strict BigQuery identifiers ([A-Za-z0-9_], length-bounded)
        # # at the onboarding boundary AND here before this format, so a
        # # crafted dataset name can't break out of the table reference.
        table_suffix = billing_account or "<ACCT>"
        table_fqn = (
            f"`{self._project}.{dataset}."
            f"gcp_billing_export_v1_{table_suffix}`"
        )

        # # TODO(TM9 Phase A): confirm the Vertex filter literal + that
        # # grouping/credits/labels match the live schema. The SELECT
        # # aliases below are pinned to BillingRow's field names.
        sql = f"""
            SELECT
              DATE(usage_start_time)        AS usage_day,
              service.description           AS service_description,
              sku.id                        AS sku_id,
              sku.description               AS sku_description,
              project.id                    AS project_id,
              project.name                  AS project_name,
              ANY_VALUE(labels)             AS labels,
              SUM(cost)                     AS cost,
              ANY_VALUE(currency)           AS currency,
              ARRAY_CONCAT_AGG(credits)     AS credits,
              SUM(usage.amount)             AS usage_amount,
              ANY_VALUE(usage.unit)         AS usage_unit
            FROM {table_fqn}
            WHERE usage_start_time >= @since
              AND usage_start_time <  @until
              -- TODO(TM9 Phase A): confirm this literal selects Vertex.
              AND service.description = @vertex_service
            GROUP BY
              usage_day, service_description, sku_id, sku_description,
              project_id, project_name
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(
                    "since", "TIMESTAMP", since
                ),
                bigquery.ScalarQueryParameter(
                    "until", "TIMESTAMP", until
                ),
                # # TODO(TM9 Phase A): replace placeholder with the
                # # confirmed Vertex service.description literal.
                bigquery.ScalarQueryParameter(
                    "vertex_service", "STRING", "Vertex AI"
                ),
            ]
        )

        try:
            job = self._client.query(sql, job_config=job_config)
            rows = job.result()
        except (Forbidden, _GPermissionDenied) as exc:
            raise PermissionDenied(
                "service account lacks BigQuery read on the billing "
                f"export dataset {dataset!r}",
                resource=table_fqn,
            ) from exc

        # ``dict(row)`` turns a BigQuery Row into a plain mapping the flat
        # Pydantic model validates; nested labels/credits arrive as
        # lists of dicts that the sub-models absorb.
        return [BillingRow.model_validate(dict(row)) for row in rows]


class VertexMonitoringClient:
    """Cloud Monitoring reader for the ``token_count`` publisher metric.

    Holds a ``monitoring_v3.MetricServiceClient`` built with the tenant's
    minted ``credentials``; ``project`` is the metrics-scope project whose
    ``projects.timeSeries.list`` is queried.
    """

    def __init__(self, credentials: Any, project: str) -> None:
        if not project:
            raise ValueError("project required")
        self._project = project
        self._project_name = f"projects/{project}"
        self._client = monitoring_v3.MetricServiceClient(
            credentials=credentials
        )

    def close(self) -> None:
        # MetricServiceClient owns a gRPC transport; closing it releases
        # the channel. ``transport.close()`` is the documented path.
        transport = getattr(self._client, "transport", None)
        if transport is not None:
            transport.close()

    def __enter__(self) -> "VertexMonitoringClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def list_token_usage(
        self, since: datetime, until: datetime
    ) -> list[TokenUsagePoint]:
        """List ``token_count`` time-series points for ``[since, until)``.

        Calls ``projects.timeSeries.list`` for the publisher
        :data:`TOKEN_COUNT_METRIC` over the interval, with a daily
        alignment, and flattens each returned ``TimeSeries`` × ``point``
        into one :class:`TokenUsagePoint` carrying the resolved
        dimensions (model / project / location / type) and the token
        count.

        Raises :class:`PermissionDenied` when the SA lacks
        ``monitoring.timeSeries.list`` on the project (per-stream
        soft-skip signal).

        # TODO(TM9 Phase A): confirm against a live project:
        #   - the metric ``filter`` string (metric type + any required
        #     resource.type clause);
        #   - the aggregation (ALIGN_SUM over a 86400s alignment period?
        #     and whether token_count is DELTA/CUMULATIVE/GAUGE, which
        #     decides the aligner);
        #   - the metric/resource label NAMES the flattening reads
        #     (``model``, ``type``, ``location``, project) — pinned to
        #     ``TokenUsagePoint`` field names here as a best guess.
        """
        # Monitoring v3 interval + aggregation. Built defensively so the
        # scaffold compiles; the exact aligner/period is a Phase-A TODO.
        interval = monitoring_v3.TimeInterval(
            start_time=since,
            end_time=until,
        )
        # # TODO(TM9 Phase A): confirm aligner + alignment_period.
        aggregation = monitoring_v3.Aggregation(
            alignment_period={"seconds": 86400},  # 1 day
            per_series_aligner=(
                monitoring_v3.Aggregation.Aligner.ALIGN_SUM
            ),
        )

        # # TODO(TM9 Phase A): confirm this filter selects the right
        # # series (metric type + optional resource.type).
        metric_filter = f'metric.type = "{TOKEN_COUNT_METRIC}"'

        request = monitoring_v3.ListTimeSeriesRequest(
            name=self._project_name,
            filter=metric_filter,
            interval=interval,
            aggregation=aggregation,
            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        )

        try:
            series_iter = self._client.list_time_series(request=request)
            return list(self._flatten_series(series_iter))
        except (Forbidden, _GPermissionDenied) as exc:
            raise PermissionDenied(
                "service account lacks Cloud Monitoring read on project "
                f"{self._project!r}",
                resource=self._project_name,
            ) from exc

    @staticmethod
    def _flatten_series(series_iter: Any) -> Any:
        """Yield one :class:`TokenUsagePoint` per (series, point).

        Each ``TimeSeries`` carries a ``metric`` (type + labels), a
        ``resource`` (type + labels), and a list of ``points`` (each an
        interval + a typed value). This resolves the dimensions off the
        metric/resource labels and emits one row per point.

        # TODO(TM9 Phase A): the label-key reads below are the recon's
        # best guess — confirm where ``model`` / ``type`` / ``location``
        # / project actually live (metric.labels vs resource.labels) and
        # which value type ``point.value`` uses (``int64_value`` for a
        # count) against a live response.
        """
        for series in series_iter:
            metric_labels = dict(getattr(series.metric, "labels", {}) or {})
            resource_labels = dict(
                getattr(series.resource, "labels", {}) or {}
            )

            model = metric_labels.get("model") or resource_labels.get(
                "model"
            )
            token_type = metric_labels.get("type")
            location = resource_labels.get("location")
            project_id = resource_labels.get("project_id") or (
                resource_labels.get("project")
            )

            for point in getattr(series, "points", []) or []:
                interval_end = None
                point_interval = getattr(point, "interval", None)
                if point_interval is not None:
                    interval_end = getattr(point_interval, "end_time", None)

                # int64_value is the count type for token_count;
                # getattr-guarded so a different value type doesn't crash
                # the scaffold (Phase A confirms the real field).
                value = getattr(
                    getattr(point, "value", None), "int64_value", 0
                )

                yield TokenUsagePoint.model_validate(
                    {
                        "model": model,
                        "project_id": project_id,
                        "location": location,
                        "token_type": token_type,
                        "usage_day": (
                            interval_end.isoformat()
                            if interval_end is not None
                            and hasattr(interval_end, "isoformat")
                            else None
                        ),
                        "token_count": int(value or 0),
                    }
                )
