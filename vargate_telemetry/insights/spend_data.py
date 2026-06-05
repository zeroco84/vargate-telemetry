# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Shared spend / cost primitives for the Insights cards (TM7).

Every card draws from the same captured Admin-API usage records that
``api/usage.py`` and ``budgets/spend.py`` read — ``record_type =
'usage'`` AND ``source_api = 'admin'``, with the per-model /
per-workspace breakdown living in ``metadata->'results'`` (a JSONB
array). This module owns the read shapes the cards need:

- :func:`daily_spend` — USD per UTC day, ascending.
- :func:`model_share` — model → (USD, share) over a window, with an
  optional ``offset_days`` so a card can compare "this week" against
  "the week before".
- :func:`workspace_spend` — USD per workspace (only rows that carry a
  ``workspace_id``), sorted by spend.
- :func:`project_period_end` — a never-raises month-to-date forecast
  built on a hand-rolled least-squares fit over the trailing 14 days.

Cross-vendor (TM8 Phase D)
--------------------------

The four functions above are the **Anthropic-only** primitives (they
hardcode ``source_api = 'admin'``) and are unchanged — every existing
caller keeps its exact behavior. TM8 adds a parallel, **additive**
per-vendor layer that does NOT touch them:

- :func:`vendor_spend_breakdown` — ``vendor -> VendorSpend`` over a
  window: Anthropic spend is usage-token-**estimated** (same numbers as
  :func:`daily_spend`); OpenAI spend is **authoritative** from the
  ``openai_admin_costs`` stream (``amount.value``) when present, else
  usage-estimated — and each :class:`VendorSpend` carries a ``basis``
  flag so the UI can label which is which.
- :func:`vendor_daily_spend` — the per-vendor daily series feeding a
  vendor-stacked forecast chart.

Both route usage-derived figures through
:func:`vendor_cost.estimate_record_cost_usd` (the cross-vendor cost
primitive) rather than re-implementing each vendor's token extraction.

Each function opens its own :func:`session_scope` so a card can call
them independently without threading a session around. Cost is always
USD via :func:`compute_cost_usd`, computed once per bucket against the
bucket's earliest ``occurred_at`` (so the rate card that was active
then is used). A plain ``SUM`` per bucket is fine here — the
supersession dedup that ``api/usage.py`` does for the precise billing
view is more than insights need, and we deliberately don't replicate
it (see module note below).

Note on supersession
--------------------

``api/usage.py`` and ``budgets/spend.py`` hide legacy aggregate rows
(``model = null``) on any UTC date that also has per-model breakdown
rows, so the two shapes don't double-count to the cent. Insights are
directional, not billing-grade: we ``GROUP BY`` model / workspace /
day and price each bucket, and a ``model IS NULL`` bucket simply
prices to ``None`` (``compute_cost_usd`` returns ``None`` for a null
model) and contributes nothing. That keeps the SQL legible without
materially moving the headline numbers a card shows.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import text as sql_text

from vargate_telemetry.budgets import compute_spend_in_window
from vargate_telemetry.db import session_scope
from vargate_telemetry.pricing import compute_cost_usd
from vargate_telemetry.pricing.vendor_cost import (
    VENDOR_ANTHROPIC,
    VENDOR_OPENAI,
    estimate_record_cost_usd,
)

_log = logging.getLogger(__name__)


# Supported insight windows → trailing-day count. Unknown windows
# fall back to 7 days (see ``window_to_days``).
WINDOW_DAYS: dict[str, int] = {"7d": 7, "30d": 30}


def window_to_days(window: str) -> int:
    """Trailing-day count for an insight ``window`` string.

    Defaults to 7 for anything not in :data:`WINDOW_DAYS` — an
    unknown window should degrade to the most common view, not raise.
    """
    return WINDOW_DAYS.get(window, 7)


# ───────────────────────────────────────────────────────────────────────────
# Shared SQL fragment
# ───────────────────────────────────────────────────────────────────────────
#
# Token + dimension extraction from one expanded ``metadata->'results'``
# element. Mirrors the COALESCE / NULLIF cache-creation handling in
# ``api/usage.py`` so a card prices the same token totals the table
# does. ``r(result)`` is the CROSS JOIN alias every query below uses.

_TOKEN_SELECT = """
    r.result->>'model' AS model,
    r.result->>'workspace_id' AS workspace_id,
    COALESCE((r.result->>'input_tokens')::bigint, 0) AS input_tokens,
    COALESCE((r.result->>'output_tokens')::bigint, 0) AS output_tokens,
    COALESCE((r.result->>'cache_read_input_tokens')::bigint, 0)
        AS cache_read_tokens,
    COALESCE(
        NULLIF((r.result->>'cache_creation_input_tokens')::bigint, 0),
        ((r.result->'cache_creation')->>'ephemeral_5m_input_tokens')::bigint
        + ((r.result->'cache_creation')->>'ephemeral_1h_input_tokens')::bigint,
        0
    ) AS cache_creation_tokens
"""

# Base record predicate shared by every insights query. RLS via
# ``session_scope`` already pins the tenant; this restricts to the
# Admin-API usage records the cards analyse.
_BASE_WHERE = """
    tr.tenant_id = current_setting('app.tenant_id')
    AND tr.record_type = 'usage'
    AND tr.source_api = 'admin'
"""


def _bucket_cost(row: object) -> Optional[Decimal]:
    """Price one aggregated bucket row via ``compute_cost_usd``.

    ``row`` must expose ``model``, the four token totals, and
    ``earliest_occurred_at`` (the MIN ``occurred_at`` of the bucket,
    so the then-active rate card is used). Returns ``None`` when the
    model is null/unknown or the bucket has no rows — the caller
    decides whether to skip or floor.
    """
    occurred = getattr(row, "earliest_occurred_at", None)
    if occurred is None:
        return None
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    return compute_cost_usd(
        row.model,
        input_tokens=int(row.input_tokens),
        output_tokens=int(row.output_tokens),
        cache_read_tokens=int(row.cache_read_tokens),
        cache_creation_tokens=int(row.cache_creation_tokens),
        occurred_at=occurred,
    )


# ───────────────────────────────────────────────────────────────────────────
# Daily spend
# ───────────────────────────────────────────────────────────────────────────


def daily_spend(tenant_id: str, days: int) -> list[tuple[date, Decimal]]:
    """USD spend per UTC day over the trailing ``days``, ascending.

    One entry per day that has at least some priceable usage; days
    with no usage (or only null/unknown-model usage) are omitted
    rather than emitted as zero — a sparse series is what the
    forecast's linear fit and the trend cards expect.

    Grouped by ``(day, model)`` so each model's tokens price against
    the rate active on that day, then summed up to the day. The
    window is ``[now - days, now)`` in UTC.
    """
    sql = sql_text(
        f"""
        SELECT
            DATE(tr.occurred_at AT TIME ZONE 'UTC') AS day,
            r.result->>'model' AS model,
            MIN(tr.occurred_at) AS earliest_occurred_at,
            COALESCE(SUM((r.result->>'input_tokens')::bigint), 0)
                AS input_tokens,
            COALESCE(SUM((r.result->>'output_tokens')::bigint), 0)
                AS output_tokens,
            COALESCE(SUM((r.result->>'cache_read_input_tokens')::bigint), 0)
                AS cache_read_tokens,
            COALESCE(SUM(COALESCE(
                NULLIF((r.result->>'cache_creation_input_tokens')::bigint, 0),
                ((r.result->'cache_creation')->>'ephemeral_5m_input_tokens')::bigint
                + ((r.result->'cache_creation')->>'ephemeral_1h_input_tokens')::bigint,
                0
            )), 0) AS cache_creation_tokens
        FROM telemetry_records tr,
             jsonb_array_elements(tr.metadata->'results') AS r(result)
        WHERE {_BASE_WHERE}
          AND tr.occurred_at >= (now() AT TIME ZONE 'UTC') - make_interval(days => :days)
        GROUP BY DATE(tr.occurred_at AT TIME ZONE 'UTC'), r.result->>'model'
        ORDER BY day ASC
        """
    )

    per_day: dict[date, Decimal] = {}
    with session_scope(tenant_id) as s:
        rows = s.execute(sql, {"days": days}).all()

    for row in rows:
        cost = _bucket_cost(row)
        if cost is None:
            continue
        per_day[row.day] = per_day.get(row.day, Decimal("0")) + cost

    return [
        (day, total.quantize(Decimal("0.01")))
        for day, total in sorted(per_day.items())
    ]


# ───────────────────────────────────────────────────────────────────────────
# Model share
# ───────────────────────────────────────────────────────────────────────────


def model_share(
    tenant_id: str, days: int, offset_days: int = 0
) -> dict[str, tuple[Decimal, Decimal]]:
    """Model → (USD, share) over a trailing window.

    The window is ``[now - offset_days - days, now - offset_days)`` in
    UTC, so a caller can pass ``offset_days=days`` to read the
    immediately-preceding period and diff the two (the model-mix card
    does exactly this to spot a model whose share jumped).

    ``share`` is the model's fraction of total priceable spend in the
    window (0..1), quantized to 4 decimals. Null/unknown-model buckets
    price to ``None`` and are excluded from both the numerator and the
    denominator. Returns ``{}`` when nothing priceable falls in the
    window.
    """
    sql = sql_text(
        f"""
        SELECT
            r.result->>'model' AS model,
            MIN(tr.occurred_at) AS earliest_occurred_at,
            COALESCE(SUM((r.result->>'input_tokens')::bigint), 0)
                AS input_tokens,
            COALESCE(SUM((r.result->>'output_tokens')::bigint), 0)
                AS output_tokens,
            COALESCE(SUM((r.result->>'cache_read_input_tokens')::bigint), 0)
                AS cache_read_tokens,
            COALESCE(SUM(COALESCE(
                NULLIF((r.result->>'cache_creation_input_tokens')::bigint, 0),
                ((r.result->'cache_creation')->>'ephemeral_5m_input_tokens')::bigint
                + ((r.result->'cache_creation')->>'ephemeral_1h_input_tokens')::bigint,
                0
            )), 0) AS cache_creation_tokens
        FROM telemetry_records tr,
             jsonb_array_elements(tr.metadata->'results') AS r(result)
        WHERE {_BASE_WHERE}
          AND tr.occurred_at >= (now() AT TIME ZONE 'UTC')
              - make_interval(days => :offset_days + :days)
          AND tr.occurred_at <  (now() AT TIME ZONE 'UTC')
              - make_interval(days => :offset_days)
        GROUP BY r.result->>'model'
        """
    )

    costs: dict[str, Decimal] = {}
    with session_scope(tenant_id) as s:
        rows = s.execute(
            sql, {"days": days, "offset_days": offset_days}
        ).all()

    for row in rows:
        if not row.model:
            continue
        cost = _bucket_cost(row)
        if cost is None:
            continue
        costs[row.model] = costs.get(row.model, Decimal("0")) + cost

    total = sum(costs.values(), Decimal("0"))
    if total <= 0:
        return {}

    return {
        model: (
            usd.quantize(Decimal("0.01")),
            (usd / total).quantize(Decimal("0.0001")),
        )
        for model, usd in costs.items()
    }


# ───────────────────────────────────────────────────────────────────────────
# Workspace attribution
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class WorkspaceSpend:
    """One workspace's share of spend over a window.

    ``name`` is resolved from the ``workspaces`` side table (the same
    one Usage joins); ``None`` when the sync hasn't seen the id yet —
    the UI then falls back to the raw ``workspace_id``.
    """

    workspace_id: str
    name: Optional[str]
    usd: Decimal
    share: Decimal


def workspace_spend(tenant_id: str, days: int) -> list[WorkspaceSpend]:
    """USD per workspace over the trailing ``days``, sorted desc by USD.

    Only buckets that actually carry a ``workspace_id`` are counted
    (``result->>'workspace_id' IS NOT NULL``) — most Personal-plan
    tenants have no workspace dimension at all, and for them this
    returns ``[]`` (the card then shows its empty-state). ``share`` is
    the workspace's fraction of the counted total (0..1). The
    workspace name is resolved via a LEFT JOIN on ``workspaces``.
    """
    sql = sql_text(
        f"""
        SELECT
            r.result->>'workspace_id' AS workspace_id,
            w.name AS name,
            r.result->>'model' AS model,
            MIN(tr.occurred_at) AS earliest_occurred_at,
            COALESCE(SUM((r.result->>'input_tokens')::bigint), 0)
                AS input_tokens,
            COALESCE(SUM((r.result->>'output_tokens')::bigint), 0)
                AS output_tokens,
            COALESCE(SUM((r.result->>'cache_read_input_tokens')::bigint), 0)
                AS cache_read_tokens,
            COALESCE(SUM(COALESCE(
                NULLIF((r.result->>'cache_creation_input_tokens')::bigint, 0),
                ((r.result->'cache_creation')->>'ephemeral_5m_input_tokens')::bigint
                + ((r.result->'cache_creation')->>'ephemeral_1h_input_tokens')::bigint,
                0
            )), 0) AS cache_creation_tokens
        FROM (telemetry_records tr
              CROSS JOIN jsonb_array_elements(tr.metadata->'results') AS r(result))
        LEFT JOIN workspaces w
          ON w.tenant_id = tr.tenant_id
         AND w.workspace_id = (r.result->>'workspace_id')
        WHERE {_BASE_WHERE}
          AND (r.result->>'workspace_id') IS NOT NULL
          AND tr.occurred_at >= (now() AT TIME ZONE 'UTC') - make_interval(days => :days)
        GROUP BY r.result->>'workspace_id', w.name, r.result->>'model'
        """
    )

    # Aggregate per (workspace_id, name) across the per-model buckets.
    costs: dict[tuple[str, Optional[str]], Decimal] = {}
    with session_scope(tenant_id) as s:
        rows = s.execute(sql, {"days": days}).all()

    for row in rows:
        cost = _bucket_cost(row)
        if cost is None:
            continue
        key = (row.workspace_id, row.name)
        costs[key] = costs.get(key, Decimal("0")) + cost

    total = sum(costs.values(), Decimal("0"))
    if total <= 0:
        return []

    result = [
        WorkspaceSpend(
            workspace_id=ws_id,
            name=name,
            usd=usd.quantize(Decimal("0.01")),
            share=(usd / total).quantize(Decimal("0.0001")),
        )
        for (ws_id, name), usd in costs.items()
    ]
    result.sort(key=lambda w: w.usd, reverse=True)
    return result


# ───────────────────────────────────────────────────────────────────────────
# Month-end forecast
# ───────────────────────────────────────────────────────────────────────────


def linear_fit(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares ``(slope, intercept)`` for ``y = slope*x + intercept``.

    Hand-rolled (no numpy). With fewer than two points there is no
    line to fit, so we return ``(0.0, mean_y)`` — a flat projection at
    the average, which keeps the forecast sane on a brand-new tenant.
    A zero-variance x (all points share an x) likewise yields slope 0.
    """
    n = len(points)
    if n < 2:
        mean_y = (sum(p[1] for p in points) / n) if n else 0.0
        return (0.0, mean_y)

    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    mean_x = sum_x / n
    mean_y = sum_y / n

    # slope = Σ(x-mean_x)(y-mean_y) / Σ(x-mean_x)²
    numerator = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points)
    denominator = sum((p[0] - mean_x) ** 2 for p in points)
    if denominator == 0:
        return (0.0, mean_y)

    slope = numerator / denominator
    intercept = mean_y - slope * mean_x
    return (slope, intercept)


@dataclass(kw_only=True)
class ForecastResult:
    """Month-to-date spend + a linear projection to month-end.

    ``current_spend`` is the real month-to-date tenant spend (via
    ``compute_spend_in_window``); ``projected_end`` extends it by the
    trailing-14-day slope across the days still left in the current
    UTC month. ``slope_per_day`` is USD/day (float, from the fit);
    ``daily_series`` is the raw ``daily_spend(...)`` it was fit on so
    the card can sparkline it.

    ``kw_only`` keeps the declared field order (``daily_series`` —
    which carries a ``default_factory`` — sits before the two
    no-default ``period_*`` dates) without tripping the
    "non-default argument follows default argument" rule. Every
    construction site passes by keyword anyway.
    """

    current_spend: Decimal
    projected_end: Decimal
    slope_per_day: float
    days_remaining: int
    days_of_data: int
    daily_series: list = field(default_factory=list)
    period_start: date
    period_end: date


def project_period_end(tenant_id: str) -> ForecastResult:
    """Forecast the tenant's spend at the end of the current UTC month.

    **Never raises** — the insights aggregator already isolates card
    failures, but the forecast is consumed by more than one card, so
    it degrades to a flat, zero-slope projection on any error rather
    than propagating. On failure ``current_spend`` and
    ``projected_end`` are equal (no growth assumed) and
    ``days_of_data`` is 0.

    Method
    ------
    - ``daily_series`` = :func:`daily_spend` over the trailing 14 days.
    - ``slope_per_day`` = the least-squares slope over
      ``(index, float(usd))`` of that series.
    - ``current_spend`` = month-to-date tenant spend via
      ``compute_spend_in_window`` over ``[first instant of this UTC
      month, now)``.
    - ``days_remaining`` = whole days from today to the last day of
      the current UTC month (0 on the last day).
    - ``projected_end`` = ``current_spend + slope_per_day *
      days_remaining``.
    """
    now = datetime.now(timezone.utc)
    period_start_date = now.date().replace(day=1)
    last_day = calendar.monthrange(now.year, now.month)[1]
    period_end_date = now.date().replace(day=last_day)
    days_remaining = (period_end_date - now.date()).days

    period_start_dt = datetime(
        now.year, now.month, 1, tzinfo=timezone.utc
    )

    try:
        series = daily_spend(tenant_id, 14)
        days_of_data = len(series)

        points = [
            (float(idx), float(usd)) for idx, (_, usd) in enumerate(series)
        ]
        slope_per_day, _ = linear_fit(points)

        with session_scope(tenant_id) as s:
            current_spend = compute_spend_in_window(
                s,
                start=period_start_dt,
                end=now,
                scope_kind="tenant",
                scope_value=None,
            )

        projected_end = current_spend + Decimal(
            str(slope_per_day * days_remaining)
        )
        return ForecastResult(
            current_spend=current_spend,
            projected_end=projected_end.quantize(Decimal("0.01")),
            slope_per_day=slope_per_day,
            days_remaining=days_remaining,
            days_of_data=days_of_data,
            daily_series=series,
            period_start=period_start_date,
            period_end=period_end_date,
        )
    except Exception:
        # Forecast is best-effort; a DB hiccup must not bubble up to
        # the (multiple) cards that read it. Flat projection.
        _log.exception("project_period_end failed; returning flat forecast")
        return ForecastResult(
            current_spend=Decimal("0.00"),
            projected_end=Decimal("0.00"),
            slope_per_day=0.0,
            days_remaining=days_remaining,
            days_of_data=0,
            daily_series=[],
            period_start=period_start_date,
            period_end=period_end_date,
        )


# ───────────────────────────────────────────────────────────────────────────
# Cross-vendor spend (TM8 Phase D) — additive; the Anthropic-only
# primitives above are untouched.
# ───────────────────────────────────────────────────────────────────────────
#
# Per-vendor "best source" (TM8 conventions, "/usage and /costs are
# complementary"):
#   - Anthropic: usage-token ESTIMATED (no authoritative billed feed in
#     Ogma) — same numbers daily_spend produces.
#   - OpenAI: AUTHORITATIVE from the openai_admin_costs stream
#     (amount.value, includes non-token line items) when that stream has
#     data in the window; else fall back to the usage-token ESTIMATE
#     from openai_admin_usage. Either way LABEL which basis was used.
#
# Both paths price usage records through
# ``vendor_cost.estimate_record_cost_usd`` (Python-side, per record)
# rather than the SQL token-SUM the Anthropic primitives use. The
# numeric result for Anthropic equals ``daily_spend`` — pricing is
# linear in tokens, and within one UTC day every record shares the same
# (UTC-midnight-bounded) rate window, so summing per-record costs equals
# pricing the per-(day, model) token SUM.

# Usage-derived basis labels surfaced on VendorSpend.basis.
BASIS_ESTIMATED = "estimated"
BASIS_AUTHORITATIVE = "authoritative"

# source_api values the per-vendor roll-up reads. The Anthropic usage
# stream + the two OpenAI streams (token usage + authoritative costs).
_SOURCE_ANTHROPIC_USAGE = "admin"
_SOURCE_OPENAI_USAGE = "openai_admin_usage"
_SOURCE_OPENAI_COSTS = "openai_admin_costs"


@dataclass
class VendorSpend:
    """One vendor's spend over a window, with the basis it was derived from.

    ``vendor`` is the display name (``"Anthropic"`` / ``"OpenAI"``).
    ``usd`` is the window total, quantized to cents. ``basis`` is
    ``"estimated"`` (usage-token × rate card) or ``"authoritative"``
    (OpenAI's billed ``/costs`` amounts) — the UI labels the figure
    accordingly so customers know Anthropic is an estimate while OpenAI
    can be the real billed number. ``daily`` is the per-UTC-day series
    (ascending, sparse — days with no priceable spend omitted) so a card
    can stack the vendors on one chart.
    """

    vendor: str
    usd: Decimal
    basis: str
    daily: list[tuple[date, Decimal]] = field(default_factory=list)


def _price_usage_records_by_day(
    tenant_id: str, days: int, source_api: str
) -> dict[date, Decimal]:
    """Per-UTC-day estimated spend for one usage ``source_api`` stream.

    Fetches each record's ``(occurred_at, metadata)`` over the trailing
    ``days`` and prices it via
    :func:`vendor_cost.estimate_record_cost_usd` (which dispatches on
    ``source_api``). Records that price to ``None`` (null/unknown model,
    empty-bucket sentinels) contribute nothing — a day with no priceable
    spend is omitted, matching :func:`daily_spend`'s sparse-series
    contract.

    For ``source_api='admin'`` the totals equal :func:`daily_spend`'s:
    pricing is linear in tokens, and the per-record ``occurred_at`` lands
    in the same UTC-day rate window the SQL path's ``MIN(occurred_at)``
    picks (rate-card windows are dated at UTC-midnight boundaries, so two
    records for the same ``(day, model)`` can't straddle a rate change).

    Unlike the SQL primitives this does NOT apply the supersession
    filter — it doesn't need to: a legacy aggregate breakdown
    (``model=null``) prices to ``None`` and contributes nothing, so it
    can't double-count against the per-model rows on the same day. That
    is exactly the posture :func:`daily_spend` documents.
    """
    sql = sql_text(
        """
        SELECT tr.occurred_at, tr.metadata
        FROM telemetry_records tr
        WHERE tr.tenant_id = current_setting('app.tenant_id')
          AND tr.record_type = 'usage'
          AND tr.source_api = :source_api
          AND tr.occurred_at >= (now() AT TIME ZONE 'UTC')
              - make_interval(days => :days)
        """
    )

    per_day: dict[date, Decimal] = {}
    with session_scope(tenant_id) as s:
        rows = s.execute(
            sql, {"days": days, "source_api": source_api}
        ).all()

    for row in rows:
        occurred = row.occurred_at
        if occurred is None:
            continue
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        cost = estimate_record_cost_usd(
            source_api, row.metadata or {}, occurred
        )
        if cost is None:
            continue
        day = occurred.astimezone(timezone.utc).date()
        per_day[day] = per_day.get(day, Decimal("0")) + cost

    return per_day


def _openai_actual_spend_by_day(
    tenant_id: str, days: int
) -> dict[date, Decimal]:
    """Per-UTC-day AUTHORITATIVE OpenAI spend from the ``openai_admin_costs``
    stream.

    Sums ``metadata->>'amount_value'`` (the billed ``amount.value`` the
    cost pull stores as an exact Decimal string) per UTC day over the
    trailing ``days``. This is the real billed figure — it includes
    non-token line items (fine-tune training, etc.) a tokens×pricing
    estimate can never reproduce, which is why per-project / total OpenAI
    spend prefers it.

    Empty-bucket sentinel cost records carry ``amount_value = null`` and
    are skipped by the ``IS NOT NULL`` filter. Returns ``{}`` when the
    costs stream has no priceable rows in the window (then the caller
    falls back to the usage estimate).
    """
    sql = sql_text(
        """
        SELECT
            DATE(tr.occurred_at AT TIME ZONE 'UTC') AS day,
            COALESCE(
                SUM((tr.metadata->>'amount_value')::numeric), 0
            ) AS amount
        FROM telemetry_records tr
        WHERE tr.tenant_id = current_setting('app.tenant_id')
          AND tr.record_type = 'cost'
          AND tr.source_api = :source_api
          AND (tr.metadata->>'amount_value') IS NOT NULL
          AND tr.occurred_at >= (now() AT TIME ZONE 'UTC')
              - make_interval(days => :days)
        GROUP BY DATE(tr.occurred_at AT TIME ZONE 'UTC')
        """
    )

    per_day: dict[date, Decimal] = {}
    with session_scope(tenant_id) as s:
        rows = s.execute(
            sql, {"days": days, "source_api": _SOURCE_OPENAI_COSTS}
        ).all()

    for row in rows:
        # ``amount`` is a Decimal from psycopg's numeric adapter.
        amount = Decimal(str(row.amount))
        if amount == 0:
            # A zero-sum day is real-but-empty; skip so the series stays
            # sparse like the estimate path (and a $0 day adds no signal).
            continue
        per_day[row.day] = per_day.get(row.day, Decimal("0")) + amount

    return per_day


def _to_series(per_day: dict[date, Decimal]) -> list[tuple[date, Decimal]]:
    """Sort a ``{day: usd}`` map into an ascending, cent-quantized series."""
    return [
        (day, total.quantize(Decimal("0.01")))
        for day, total in sorted(per_day.items())
    ]


def vendor_daily_spend(
    tenant_id: str, days: int
) -> dict[str, list[tuple[date, Decimal]]]:
    """Per-vendor daily spend series over the trailing ``days``.

    Returns ``{vendor_name: [(day, usd), ...]}`` with one ascending,
    sparse series per vendor that has any priceable spend in the window
    (a vendor with none is omitted entirely — the caller decides whether
    to render an empty band). Vendors:

    - ``"Anthropic"`` — usage-token estimate (the ``admin`` stream),
      identical day-by-day to :func:`daily_spend`.
    - ``"OpenAI"`` — authoritative ``openai_admin_costs`` per-day spend
      when that stream has data in the window; otherwise the
      ``openai_admin_usage`` token estimate.

    This is the chart feed (vendor-stacked forecast); the labelled
    totals + basis live on :func:`vendor_spend_breakdown`.
    """
    out: dict[str, list[tuple[date, Decimal]]] = {}

    anthropic = _price_usage_records_by_day(
        tenant_id, days, _SOURCE_ANTHROPIC_USAGE
    )
    if anthropic:
        out[VENDOR_ANTHROPIC] = _to_series(anthropic)

    openai_actual = _openai_actual_spend_by_day(tenant_id, days)
    if openai_actual:
        out[VENDOR_OPENAI] = _to_series(openai_actual)
    else:
        openai_estimate = _price_usage_records_by_day(
            tenant_id, days, _SOURCE_OPENAI_USAGE
        )
        if openai_estimate:
            out[VENDOR_OPENAI] = _to_series(openai_estimate)

    return out


def vendor_spend_breakdown(
    tenant_id: str, days: int
) -> dict[str, VendorSpend]:
    """Per-vendor spend split over the trailing ``days``, with basis.

    The cross-vendor accessor wave-2 cost cards consume. Returns
    ``{vendor_name: VendorSpend}`` for each vendor that has any priceable
    spend in the window. Each :class:`VendorSpend` carries the window
    ``usd`` total, the per-day ``daily`` series, and a ``basis`` flag:

    - **Anthropic** → ``basis="estimated"`` (usage-token × rate card;
      Ogma has no authoritative Anthropic billing feed). Equal to
      :func:`daily_spend`'s total for the same window.
    - **OpenAI** → ``basis="authoritative"`` when the
      ``openai_admin_costs`` stream has billed amounts in the window
      (the real number, incl. non-token line items); otherwise
      ``basis="estimated"`` from the ``openai_admin_usage`` token counts.

    A vendor with no priceable spend is omitted (not emitted as a $0
    entry) — same sparse posture as the daily series. Pure read; never
    raises on a missing stream (an absent stream is simply an empty map).
    """
    out: dict[str, VendorSpend] = {}

    # ── Anthropic: usage-token estimate ──
    anthropic_daily = _price_usage_records_by_day(
        tenant_id, days, _SOURCE_ANTHROPIC_USAGE
    )
    if anthropic_daily:
        series = _to_series(anthropic_daily)
        out[VENDOR_ANTHROPIC] = VendorSpend(
            vendor=VENDOR_ANTHROPIC,
            usd=sum((u for _, u in series), Decimal("0")).quantize(
                Decimal("0.01")
            ),
            basis=BASIS_ESTIMATED,
            daily=series,
        )

    # ── OpenAI: authoritative /costs preferred, else usage estimate ──
    openai_actual = _openai_actual_spend_by_day(tenant_id, days)
    if openai_actual:
        series = _to_series(openai_actual)
        out[VENDOR_OPENAI] = VendorSpend(
            vendor=VENDOR_OPENAI,
            usd=sum((u for _, u in series), Decimal("0")).quantize(
                Decimal("0.01")
            ),
            basis=BASIS_AUTHORITATIVE,
            daily=series,
        )
    else:
        openai_daily = _price_usage_records_by_day(
            tenant_id, days, _SOURCE_OPENAI_USAGE
        )
        if openai_daily:
            series = _to_series(openai_daily)
            out[VENDOR_OPENAI] = VendorSpend(
                vendor=VENDOR_OPENAI,
                usd=sum((u for _, u in series), Decimal("0")).quantize(
                    Decimal("0.01")
                ),
                basis=BASIS_ESTIMATED,
                daily=series,
            )

    return out
