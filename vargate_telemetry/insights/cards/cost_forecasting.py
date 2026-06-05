# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Cost-forecasting insight card (TM7, cross-vendor in TM8 Phase D).

Projects the tenant's end-of-month spend from the trailing-14-day
trend and compares it against active **monthly, tenant-scope** budget
caps.

Cross-vendor (TM8 Phase D)
--------------------------

The headline projection is now the **total across all vendors**; the
card body breaks the projection down **per vendor** ("Anthropic: $X
projected · OpenAI: $Z within cap"), each line labelled with the basis
it was derived from:

- **Anthropic** is usage-token *estimated* — the exact same projection
  :func:`spend_data.project_period_end` produces (its month-to-date
  ``current_spend`` + the trailing-14-day linear-fit slope across the
  days left in the month). That primitive is reused unchanged, so a
  tenant with only Anthropic spend sees the byte-for-byte TM7 number.
- **OpenAI** is *authoritative* (billed) when the ``openai_admin_costs``
  stream has data in the window, otherwise usage-token *estimated* —
  via :func:`spend_data.vendor_spend_breakdown` /
  :func:`spend_data.vendor_daily_spend`. OpenAI gets the same linear-fit
  method: month-to-date level + trailing-14-day slope × days remaining.

Budgets are tenant-scope monthly caps (they cap *total* spend), so the
breach comparison runs against the **total** projected figure.

Render states (unchanged in shape):

- **Not enough history** (combined ``days_of_data < 7``): an idle card
  asking for more data. No CTA.
- **A cap is on track to be exceeded**: an *advisory* card naming the
  worst-offending budget (highest projected/threshold ratio), the
  current vs. projected (total) spend, the per-vendor breakdown, and
  the days to breach. CTA to the forecast detail page.
- **A projection exists but no cap is exceeded** (or no cap set): an
  idle, finding-free card that still surfaces the projection sentence
  + per-vendor breakdown + CTA, so the operator can always see where
  the month is heading.

The per-vendor projection series ALSO feeds the ``/insights/forecast``
drill-in chart (vendor-stacked) via :func:`vendor_forecasts`, which the
route imports.

Money is formatted as whole dollars with thousands separators
(``"$48"`` / ``"$1,240"``); only the slope math drops to ``float``.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import text as sql_text

from vargate_telemetry.db import session_scope
from vargate_telemetry.insights import spend_data
from vargate_telemetry.insights.models import (
    Card,
    InsightCta,
    InsightItem,
    idle_card,
)
from vargate_telemetry.pricing.vendor_cost import (
    SOURCE_API_OPENAI_USAGE,
    estimate_record_cost_usd,
)

CARD_ID = "cost_forecasting"
CARD_TITLE = "Cost forecasting"

# Where the card's CTA points — the forecast detail view.
_FORECAST_HREF = "/insights/forecast"

# Trailing window the per-vendor slope is fit over — matches the
# 14 days project_period_end uses for the Anthropic fit.
_FIT_DAYS = 14


def _usd(amount: Decimal) -> str:
    """Format a Decimal as whole dollars with thousands separators.

    ``Decimal("1240.50") -> "$1,240"``. Rounds to the nearest whole
    dollar; the cards are directional, not billing-grade.
    """
    return "$" + format(int(amount.quantize(Decimal("1"))), ",d")


# ───────────────────────────────────────────────────────────────────────────
# Per-vendor projection (TM8 Phase D)
# ───────────────────────────────────────────────────────────────────────────


@dataclass(kw_only=True)
class VendorForecast:
    """One vendor's month-to-date spend + a linear projection to month-end.

    Mirrors the fields :class:`spend_data.ForecastResult` carries, but
    per vendor and with a ``basis`` flag (``"estimated"`` /
    ``"authoritative"``) so the UI can label whether the figure is a
    usage-token estimate (Anthropic always; OpenAI when no billed
    ``/costs`` data) or OpenAI's real billed number.

    ``daily_series`` is the per-vendor trailing-window daily spend the
    slope was fit on, so the forecast-detail chart can vendor-stack the
    actuals + draw a per-vendor projection segment.
    """

    vendor: str
    basis: str
    current_spend: Decimal
    projected_end: Decimal
    slope_per_day: float
    daily_series: list = field(default_factory=list)


def _month_window() -> tuple[date, date, int]:
    """Current UTC month bounds + ``days_remaining``.

    ``days_remaining`` is whole days from today to the month's last day
    (0 on the last day), matching :func:`spend_data.project_period_end`.
    """
    now = datetime.now(timezone.utc)
    period_start = now.date().replace(day=1)
    last_day = calendar.monthrange(now.year, now.month)[1]
    period_end = now.date().replace(day=last_day)
    days_remaining = (period_end - now.date()).days
    return period_start, period_end, days_remaining


def _project(
    current_spend: Decimal,
    daily_series: list,
    days_remaining: int,
) -> tuple[Decimal, float]:
    """Project ``current_spend`` to month-end via the trailing-series slope.

    Identical method to :func:`spend_data.project_period_end`: least-
    squares slope over ``(index, float(usd))`` of the trailing daily
    series, extended across ``days_remaining``. Returns
    ``(projected_end_quantized, slope_per_day)``.
    """
    points = [
        (float(idx), float(usd))
        for idx, (_, usd) in enumerate(daily_series)
    ]
    slope_per_day, _ = spend_data.linear_fit(points)
    projected = current_spend + Decimal(str(slope_per_day * days_remaining))
    return projected.quantize(Decimal("0.01")), slope_per_day


def _openai_month_to_date(tenant_id: str) -> Decimal:
    """Authoritative-preferred OpenAI spend since the start of the month.

    Sums the ``openai_admin_costs`` billed amounts for records on/after
    the first instant of the current UTC month; falls back to the
    ``openai_admin_usage`` token estimate when the costs stream has no
    billed rows this month. Anchored to the midnight month boundary so
    the level matches the month-to-date semantics
    ``project_period_end`` uses for Anthropic. Returns ``Decimal("0.00")``
    when OpenAI has no priceable spend.
    """
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    costs_sql = sql_text(
        """
        SELECT COALESCE(SUM((tr.metadata->>'amount_value')::numeric), 0)
            AS amount
        FROM telemetry_records tr
        WHERE tr.tenant_id = current_setting('app.tenant_id')
          AND tr.record_type = 'cost'
          AND tr.source_api = 'openai_admin_costs'
          AND (tr.metadata->>'amount_value') IS NOT NULL
          AND tr.occurred_at >= :month_start
        """
    )
    with session_scope(tenant_id) as s:
        billed_raw = s.execute(
            costs_sql, {"month_start": month_start}
        ).scalar()
    billed = Decimal(str(billed_raw or 0))

    if billed > 0:
        return billed.quantize(Decimal("0.01"))

    # No authoritative billed rows this month → usage-token estimate.
    usage_sql = sql_text(
        """
        SELECT tr.occurred_at, tr.metadata
        FROM telemetry_records tr
        WHERE tr.tenant_id = current_setting('app.tenant_id')
          AND tr.record_type = 'usage'
          AND tr.source_api = 'openai_admin_usage'
          AND tr.occurred_at >= :month_start
        """
    )

    total = Decimal("0")
    with session_scope(tenant_id) as s:
        rows = s.execute(usage_sql, {"month_start": month_start}).all()
    for row in rows:
        occurred = row.occurred_at
        if occurred is None:
            continue
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        cost = estimate_record_cost_usd(
            SOURCE_API_OPENAI_USAGE, row.metadata or {}, occurred
        )
        if cost is not None:
            total += cost
    return total.quantize(Decimal("0.01"))


def vendor_forecasts(tenant_id: str) -> list[VendorForecast]:
    """Per-vendor month-to-date spend + month-end projection.

    Reused by both the card and the ``/insights/forecast`` route.

    - **Anthropic** comes straight from
      :func:`spend_data.project_period_end` (unchanged TM7 math), so a
      tenant with only Anthropic spend keeps the exact TM7 number.
      Emitted only when it has ≥1 day of priceable history *or* a
      non-zero month-to-date level (a vendor with literally nothing is
      omitted — sparse, like the spend accessors).
    - **OpenAI** uses the same linear-fit method over its trailing-14
      daily series (authoritative ``/costs`` preferred, else usage
      estimate) on top of its month-to-date level. Basis is taken from
      :func:`spend_data.vendor_spend_breakdown`.

    Never raises — ``project_period_end`` is already best-effort, and the
    OpenAI reads are pure SELECTs.
    """
    _, _, days_remaining = _month_window()
    out: list[VendorForecast] = []

    # ── Anthropic: reuse the unchanged TM7 forecast verbatim ──
    res = spend_data.project_period_end(tenant_id)
    if res.days_of_data > 0 or res.current_spend > 0:
        out.append(
            VendorForecast(
                vendor=spend_data.VENDOR_ANTHROPIC,
                basis=spend_data.BASIS_ESTIMATED,
                current_spend=res.current_spend,
                projected_end=res.projected_end,
                slope_per_day=res.slope_per_day,
                daily_series=res.daily_series,
            )
        )

    # ── OpenAI: same method, vendor accessors for the inputs ──
    breakdown = spend_data.vendor_spend_breakdown(tenant_id, _FIT_DAYS)
    openai_window = breakdown.get(spend_data.VENDOR_OPENAI)
    series_by_vendor = spend_data.vendor_daily_spend(tenant_id, _FIT_DAYS)
    openai_series = series_by_vendor.get(spend_data.VENDOR_OPENAI, [])
    openai_mtd = _openai_month_to_date(tenant_id)

    # Emit OpenAI when it has any priceable trailing spend or a non-zero
    # month-to-date level. ``basis`` follows the windowed breakdown (which
    # decides authoritative-vs-estimate); default to estimated if the
    # trailing window is empty but month-to-date isn't.
    if openai_window is not None or openai_mtd > 0:
        basis = (
            openai_window.basis
            if openai_window is not None
            else spend_data.BASIS_ESTIMATED
        )
        projected, slope = _project(openai_mtd, openai_series, days_remaining)
        out.append(
            VendorForecast(
                vendor=spend_data.VENDOR_OPENAI,
                basis=basis,
                current_spend=openai_mtd,
                projected_end=projected,
                slope_per_day=slope,
                daily_series=openai_series,
            )
        )

    return out


def _basis_label(basis: str) -> str:
    """Short parenthetical for a vendor's basis, e.g. ``"estimated"``."""
    return (
        "authoritative"
        if basis == spend_data.BASIS_AUTHORITATIVE
        else "estimated"
    )


def _vendor_item(vf: VendorForecast, cap: Optional[Decimal]) -> InsightItem:
    """One per-vendor projection line for the card body.

    ``"Anthropic: $X projected (~$Y over)"`` when this vendor alone would
    clear the cap, else ``"$X projected · within cap"`` / ``"$X
    projected"`` when no cap. The basis is named in the ``detail`` so the
    operator knows whether the figure is estimated or OpenAI's billed
    number.
    """
    value = f"{_usd(vf.projected_end)} projected"
    detail = f"{_basis_label(vf.basis)}"
    if cap is not None and vf.projected_end > cap:
        value += f" (~{_usd(vf.projected_end - cap)} over)"
    elif cap is not None:
        detail += " · within cap"
    return InsightItem(label=vf.vendor, value=value, detail=detail)


def build_card(tenant_id: str, window: str) -> Card:
    forecasts = vendor_forecasts(tenant_id)

    # Combined history + totals across vendors. ``days_of_data`` is the
    # max any single vendor has — the projection is meaningful once ANY
    # vendor has enough trend, and the Anthropic-only path keeps the
    # exact TM7 gate (its days_of_data is the only contributor then).
    days_of_data = max(
        (len(vf.daily_series) for vf in forecasts), default=0
    )
    total_current = sum(
        (vf.current_spend for vf in forecasts), Decimal("0")
    )
    total_projected = sum(
        (vf.projected_end for vf in forecasts), Decimal("0")
    )
    total_slope = sum((vf.slope_per_day for vf in forecasts), 0.0)

    # ── Not enough history to project ──────────────────────────────
    if days_of_data < 7:
        return idle_card(
            CARD_ID,
            CARD_TITLE,
            empty_state=(
                "Need at least 7 days of spend data to project -- we "
                f"currently have {days_of_data}. Projection uses the "
                "last 14 days as a trend; actual spend may vary with usage "
                "patterns."
            ),
        )

    # Active monthly, tenant-scope budgets for this tenant. RLS via
    # session_scope already pins tenant_id; we further restrict to the
    # live (non-deleted) monthly tenant-wide caps the forecast can be
    # compared against. Caps are tenant-scope → compared against the
    # cross-vendor TOTAL projection.
    sql = sql_text(
        """
        SELECT id, name, threshold_usd
        FROM budgets
        WHERE deleted_at IS NULL
          AND scope_kind = 'tenant'
          AND period = 'monthly'
        """
    )
    with session_scope(tenant_id) as s:
        budget_rows = s.execute(sql).all()

    monthly_thresholds = [row.threshold_usd for row in budget_rows]
    smallest_cap = min(monthly_thresholds) if monthly_thresholds else None

    # ── A cap is projected to be exceeded ──────────────────────────
    exceeding = [
        row for row in budget_rows if total_projected > row.threshold_usd
    ]
    if exceeding:
        # Worst offender = highest projected/threshold ratio.
        worst = max(
            exceeding,
            key=lambda row: total_projected / row.threshold_usd,
        )
        overage = total_projected - worst.threshold_usd

        # Days to breach is only meaningful while spend is growing. A
        # flat/declining combined slope (<= 0) means the cap is exceeded
        # purely by month-to-date level, not by a future trend crossing,
        # so there's no breach date to compute.
        days_to_breach: Optional[int]
        breach_date: Optional[date]
        if total_slope > 0:
            days_to_breach = max(
                0,
                round(
                    (worst.threshold_usd - total_current)
                    / Decimal(str(total_slope))
                ),
            )
            # today + days, never past the end of the current month.
            breach_date = min(
                date.today() + timedelta(days=days_to_breach),
                _month_window()[1],
            )
        else:
            days_to_breach = None
            breach_date = None

        items = [
            InsightItem(
                label="Current spend",
                value=f"{_usd(total_current)} of "
                f"{_usd(worst.threshold_usd)}",
            ),
            InsightItem(
                label="Projected end of period",
                value=f"~{_usd(total_projected)}",
            ),
        ]

        if days_to_breach is not None:
            unit = "day" if days_to_breach == 1 else "days"
            items.append(
                InsightItem(
                    label="Days to threshold breach",
                    value=f"{days_to_breach} {unit}",
                    detail=f"around {breach_date.strftime('%b %-d')}",
                )
            )

        # Per-vendor breakdown lines (only when more than one vendor has
        # spend — a single-vendor tenant gains nothing from a one-row
        # breakdown that just restates the total).
        if len(forecasts) > 1:
            items.extend(
                _vendor_item(vf, worst.threshold_usd) for vf in forecasts
            )

        headline = f'On track to exceed "{worst.name}" by ~{_usd(overage)} '
        if breach_date is not None:
            headline += f"on {breach_date.strftime('%b %-d')}"
        else:
            headline += "this month"

        return Card(
            id=CARD_ID,
            title=CARD_TITLE,
            severity="advisory",
            findings_count=len(exceeding),
            headline=headline,
            items=items,
            empty_state=None,
            cta=InsightCta(label="See projection", href=_FORECAST_HREF),
        )

    # ── Projection exists, no cap exceeded ─────────────────────────
    # idle_card can't carry a CTA, so build the Card by hand. We still
    # want the projection sentence + per-vendor breakdown + CTA visible.
    if smallest_cap is not None:
        cap_clause = f", within your {_usd(smallest_cap)} cap."
    else:
        cap_clause = " (no monthly cap set to compare)."

    empty_state = (
        f"On current pace you will spend ~{_usd(total_projected)} this "
        f"month{cap_clause} Projection uses the last 14 days as a trend; "
        "actual spend may vary."
    )

    # Per-vendor breakdown still rides along (zero findings, but the
    # operator sees the cross-vendor split). Skipped for a single vendor.
    items = (
        [_vendor_item(vf, smallest_cap) for vf in forecasts]
        if len(forecasts) > 1
        else []
    )

    return Card(
        id=CARD_ID,
        title=CARD_TITLE,
        severity="idle",
        findings_count=0,
        headline="",
        items=items,
        empty_state=empty_state,
        cta=InsightCta(label="See projection", href=_FORECAST_HREF),
    )
