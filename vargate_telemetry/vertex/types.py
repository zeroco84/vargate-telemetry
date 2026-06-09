# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Pydantic models for the Google Vertex AI ingest shapes (TM9 scaffold).

Two source surfaces, two row shapes (audit is DEFERRED — not modeled
here):

  - ``BillingRow`` — one grouped row from the BigQuery **billing export**
    (``gcp_billing_export_v1_<ACCT>``), at day / SKU / project / labels
    grain, with credits netted. The authoritative billed-spend stream
    (``source_api "vertex_billing_costs"``).
  - ``TokenUsagePoint`` — one Cloud Monitoring time-series point for the
    ``token_count`` metric, at day / model / project / type
    (input|output) grain. The token-usage stream
    (``source_api "vertex_token_usage"``), which the cross-vendor cost
    primitive prices into an *estimate*.

Per the multi-vendor convention (CLAUDE.md "TM8 conventions") and the
``flat_model_with_extra_allow_over_discriminated_union`` rule, every
model is a **flat** Pydantic model with ``ConfigDict(extra="allow")``:
the BigQuery row schema and the Monitoring point shape can both gain
fields without a versioned response-shape change, so the conservative
posture is absorb-and-keep (the unmodeled field lands in ``model_extra``)
rather than refuse to parse.

Pydantic 2.x reserves the ``model_`` namespace; any model with a field
literally named ``model`` sets ``protected_namespaces=()`` so the field
keeps its natural wire name (``TokenUsagePoint.model``).

Money note: ``BillingRow.cost`` and ``Credit.amount`` arrive from
BigQuery as NUMERIC (or a JSON number). They are parsed via
``Decimal(str(value))`` in a validator so billed spend never passes
through a binary float — same discipline as
``openai.types.CostAmount.value``. Token counts stay ``int``.

Attribution note (LOCKED): Google has **no per-user-email attribution**.
There is no user field on either shape — dims are project (id/name) and
team (request labels) ONLY. Do NOT add a ``user_email`` here; the
cross-vendor email reconciler is intentionally untouched for Google.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator


# ───────────────────────────────────────────────────────────────────────────
# Billing export — BigQuery gcp_billing_export_v1_<ACCT>  (desk recon §1)
#
# One queried row is grouped by day (DATE(usage_start_time)) / sku.id /
# project.id / a hash of request labels, with cost summed and credits
# netted. The Vertex slice is filtered by service.description.
# TODO(TM9 Phase A): confirm the exact column set + types against a real
# export table — the billing export schema is well-documented but the
# label-array flattening + credits-array unnest are confirmed only by
# desk recon here.
# ───────────────────────────────────────────────────────────────────────────


class Label(BaseModel):
    """One ``key``/``value`` pair from a billing row's ``labels[]`` array.

    Request labels are the **team** attribution dim for Google (LOCKED:
    project/team only, no per-user). BigQuery returns ``labels`` as a
    repeated ``STRUCT<key STRING, value STRING>``; the client flattens /
    re-emits the subset it groups on. ``extra="allow"`` absorbs any
    additional struct fields a future export schema adds.
    """

    model_config = ConfigDict(extra="allow")

    key: Optional[str] = None
    value: Optional[str] = None


class Credit(BaseModel):
    """One entry from a billing row's ``credits[]`` array.

    Recon §1: net cost = ``SUM(cost) + SUM(credits.amount)`` — credit
    amounts are NEGATIVE in the export, so ADDING them nets the discount
    (committed-use discounts, free-tier, promotions). ``amount`` is
    parsed via ``Decimal(str(value))`` to avoid float drift.
    TODO(TM9 Phase A): confirm the credit struct fields (``amount`` +
    ``type``/``name``/``id``) against a real export row.
    """

    model_config = ConfigDict(extra="allow")

    amount: Decimal = Decimal("0")
    name: Optional[str] = None
    type: Optional[str] = None

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_via_str_decimal(cls, v: Any) -> Any:
        """Decimal(str(value)) — survive sci-notation + avoid binary float.

        Mirrors ``openai.types.CostAmount._value_via_str_decimal``:
        ``None`` / already-``Decimal`` pass through; a ``float`` (incl.
        sci-notation like ``-1.29e-05``) stringifies losslessly for
        ``Decimal``; ints / numeric strings are exact via ``str()``.
        """
        if v is None:
            return v
        if isinstance(v, Decimal):
            return v
        if isinstance(v, float):
            return Decimal(str(v))
        return Decimal(str(v))


class BillingRow(BaseModel):
    """One grouped row of Vertex billed spend from the BigQuery export.

    Grain (recon §1): day / SKU / project / request-label set. The
    client's ``query_costs`` SQL groups by these and sums ``cost`` +
    unnests ``credits``. ``cost`` is the gross summed cost; ``credits``
    carries the (negative) credit entries to net against it — the pull
    task computes ``net = cost + SUM(credits.amount)``.

    Field provenance (recon §1, all MEDIUM-confidence until Phase A):
      - ``usage_day`` — ``DATE(usage_start_time)``; the day bucket the
        cost rolls up to.
      - ``service_description`` — e.g. the Vertex AI service label;
        the row is already filtered to Vertex by the WHERE clause, but
        it rides along for the dashboard.
      - ``sku_id`` / ``sku_description`` — the billed SKU (per-model
        input/output token SKUs, plus non-token SKUs).
      - ``project_id`` / ``project_name`` — the **project** attribution
        dim (LOCKED: project/team only).
      - ``labels`` — the request labels (the **team** dim).
      - ``cost`` — gross summed cost (Decimal).
      - ``currency`` — billing currency (``"USD"`` expected).
      - ``credits`` — the credit entries to net.
      - ``usage_amount`` / ``usage_unit`` — billed usage quantity
        (informational; token counts come from the Monitoring stream,
        not here).

    TODO(TM9 Phase A): confirm field presence + the exact
    ``service.description`` literal used to filter Vertex rows (the
    "Gemini Enterprise Agent Platform" rebrand may have changed it).
    """

    model_config = ConfigDict(extra="allow")

    usage_day: Optional[str] = None
    service_description: Optional[str] = None
    sku_id: Optional[str] = None
    sku_description: Optional[str] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    labels: list[Label] = []
    cost: Decimal = Decimal("0")
    currency: Optional[str] = None
    credits: list[Credit] = []
    usage_amount: Optional[float] = None
    usage_unit: Optional[str] = None

    @field_validator("cost", mode="before")
    @classmethod
    def _cost_via_str_decimal(cls, v: Any) -> Any:
        """Decimal(str(value)) for ``cost`` — as in ``Credit.amount``."""
        if v is None:
            return v
        if isinstance(v, Decimal):
            return v
        if isinstance(v, float):
            return Decimal(str(v))
        return Decimal(str(v))


# ───────────────────────────────────────────────────────────────────────────
# Token usage — Cloud Monitoring projects.timeSeries.list  (desk recon §2)
#
# Metric type aiplatform.googleapis.com/publisher/online_serving/
# token_count, label "type" (input|output), per model / location /
# project. We read aligned daily points and emit one TokenUsagePoint per
# (day, model, project, type).
# TODO(TM9 Phase A): confirm the exact metric type + resource/metric
# label names + the right aggregation (ALIGN_SUM over a 1-day alignment
# period) against a live project's Monitoring API.
# ───────────────────────────────────────────────────────────────────────────


class TokenUsagePoint(BaseModel):
    """One daily Cloud Monitoring ``token_count`` point.

    Grain (recon §2): day / model / project / type (input|output). The
    client's ``list_token_usage`` reads the time series with a 1-day
    alignment and flattens each (series, point) into one of these.

    Field provenance (recon §2, MEDIUM-confidence until Phase A):
      - ``usage_day`` — the aligned interval's end (or start) date.
      - ``model`` — the publisher model (e.g. ``gemini-2.5-pro``), read
        from a metric/resource label. ``protected_namespaces=()`` keeps
        the natural ``model`` wire name (Pydantic reserves ``model_``).
        Feeds the rate-card lookup in ``pricing/vertex_rates.py``.
      - ``project_id`` — the monitored project (the **project** dim).
      - ``location`` — the model serving region (informational).
      - ``token_type`` — the ``type`` metric label: ``"input"`` or
        ``"output"``. The pull task pivots input vs output into the
        cost estimate.
      - ``token_count`` — the summed token count for the
        (day, model, project, type) point (int).

    No user field — Google has no per-user attribution (LOCKED).
    TODO(TM9 Phase A): confirm whether ``token_count`` is a delta/cumul
    metric (affects the aligner) and whether cached-token counts are a
    separate metric or a label value.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    usage_day: Optional[str] = None
    model: Optional[str] = None
    project_id: Optional[str] = None
    location: Optional[str] = None
    token_type: Optional[str] = None
    token_count: int = 0
