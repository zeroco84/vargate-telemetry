# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Cost-forecasting insight card (TM7).

Projects the tenant's end-of-month spend from the trailing-14-day
trend and compares it against active **monthly, tenant-scope** budget
caps. The projection itself comes from
:func:`spend_data.project_period_end`, which never raises and floors
to a flat (zero-slope) forecast on any error.

Three render states:

- **Not enough history** (``days_of_data < 7``): an idle card asking
  for more data. No CTA — there is nothing to project yet.
- **A cap is on track to be exceeded**: an *advisory* card naming the
  worst-offending budget (highest projected/threshold ratio), the
  current vs. projected spend, and the days to breach. CTA to the
  forecast detail page.
- **A projection exists but no cap is exceeded** (or no cap set): an
  idle, finding-free card that still surfaces the projection sentence
  + CTA, so the operator can always see where the month is heading.
  Built as a :class:`Card` directly rather than via :func:`idle_card`
  because that helper can't carry a CTA.

Money is formatted as whole dollars with thousands separators
(``"$48"`` / ``"$1,240"``); only the slope math drops to ``float``.
"""

from __future__ import annotations

from datetime import date, timedelta
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

CARD_ID = "cost_forecasting"
CARD_TITLE = "Cost forecasting"

# Where the card's CTA points — the forecast detail view.
_FORECAST_HREF = "/insights/forecast"


def _usd(amount: Decimal) -> str:
    """Format a Decimal as whole dollars with thousands separators.

    ``Decimal("1240.50") -> "$1,240"``. Rounds to the nearest whole
    dollar; the cards are directional, not billing-grade.
    """
    return "$" + format(int(amount.quantize(Decimal("1"))), ",d")


def build_card(tenant_id: str, window: str) -> Card:
    res = spend_data.project_period_end(tenant_id)

    # ── Not enough history to project ──────────────────────────────
    if res.days_of_data < 7:
        return idle_card(
            CARD_ID,
            CARD_TITLE,
            empty_state=(
                "Need at least 7 days of spend data to project -- we "
                f"currently have {res.days_of_data}. Projection uses the "
                "last 14 days as a trend; actual spend may vary with usage "
                "patterns."
            ),
        )

    # Active monthly, tenant-scope budgets for this tenant. RLS via
    # session_scope already pins tenant_id; we further restrict to the
    # live (non-deleted) monthly tenant-wide caps the forecast can be
    # compared against.
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

    # ── A cap is projected to be exceeded ──────────────────────────
    exceeding = [
        row for row in budget_rows if res.projected_end > row.threshold_usd
    ]
    if exceeding:
        # Worst offender = highest projected/threshold ratio.
        worst = max(
            exceeding,
            key=lambda row: res.projected_end / row.threshold_usd,
        )
        overage = res.projected_end - worst.threshold_usd

        # Days to breach is only meaningful while spend is growing. A
        # flat/declining slope (<= 0) means the cap is exceeded purely
        # by month-to-date level, not by a future trend crossing, so
        # there's no breach date to compute.
        days_to_breach: Optional[int]
        breach_date: Optional[date]
        if res.slope_per_day > 0:
            days_to_breach = max(
                0,
                round(
                    (worst.threshold_usd - res.current_spend)
                    / Decimal(str(res.slope_per_day))
                ),
            )
            # today + days, never past the end of the current month.
            breach_date = min(
                date.today() + timedelta(days=days_to_breach),
                res.period_end,
            )
        else:
            days_to_breach = None
            breach_date = None

        items = [
            InsightItem(
                label="Current spend",
                value=f"{_usd(res.current_spend)} of "
                f"{_usd(worst.threshold_usd)}",
            ),
            InsightItem(
                label="Projected end of period",
                value=f"~{_usd(res.projected_end)}",
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
    # want the projection sentence + CTA visible.
    monthly_thresholds = [row.threshold_usd for row in budget_rows]
    if monthly_thresholds:
        cap = min(monthly_thresholds)
        cap_clause = f", within your {_usd(cap)} cap."
    else:
        cap_clause = " (no monthly cap set to compare)."

    empty_state = (
        f"On current pace you will spend ~{_usd(res.projected_end)} this "
        f"month{cap_clause} Projection uses the last 14 days as a trend; "
        "actual spend may vary."
    )

    return Card(
        id=CARD_ID,
        title=CARD_TITLE,
        severity="idle",
        findings_count=0,
        headline="",
        items=[],
        empty_state=empty_state,
        cta=InsightCta(label="See projection", href=_FORECAST_HREF),
    )
