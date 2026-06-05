# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Insights API (TM7) — the dashboard "what's going on" card column.

One endpoint:

- ``GET /api/insights?window=7d`` — the full ordered list of insight
  cards for the authenticated tenant over the requested window.

The heavy lifting lives in :mod:`vargate_telemetry.insights`: the
aggregator runs every registered card and isolates per-card failures
so a single bad analysis can never 500 the page. This module is just
the auth + tenant-binding wrapper.

Auth & authorization
====================

``Depends(current_user)`` gates the endpoint; a user without a bound
tenant gets a 400 with the same ``no_tenant_bound`` shape every other
tenant-scoped endpoint (``/usage``, ``/budgets``) returns. Per-card
SQL runs under ``session_scope(tenant_id)`` inside the spend-data
helpers, so RLS enforces tenant isolation.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text as sql_text

from vargate_telemetry.auth.middleware import (
    AuthenticatedUser,
    current_user,
)
from vargate_telemetry.db import session_scope
from vargate_telemetry.insights.aggregator import build_insights
from vargate_telemetry.insights.cards import cost_forecasting
from vargate_telemetry.insights.models import InsightsResponse
from vargate_telemetry.insights.spend_data import project_period_end

_log = logging.getLogger(__name__)

router = APIRouter()


def _require_tenant(user: AuthenticatedUser) -> str:
    """Reject requests from users not yet bound to a tenant.

    Same shape as ``/api/usage`` and ``/api/budgets``. Returns the
    tenant_id when present so the caller can pass it straight through
    to the aggregator.
    """
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "no_tenant_bound",
                "message": "Your session is not bound to a tenant yet.",
            },
        )
    return user.tenant_id


@router.get(
    "/insights",
    response_model=InsightsResponse,
    operation_id="getInsights",
    tags=["insights"],
    summary="Insight cards for the authenticated tenant",
)
def get_insights(
    window: str = Query("7d"),
    user: AuthenticatedUser = Depends(current_user),
) -> InsightsResponse:
    tenant_id = _require_tenant(user)
    return build_insights(tenant_id, window)


# ───────────────────────────────────────────────────────────────────────────
# Forecast detail (TM7) — GET /insights/forecast
# ───────────────────────────────────────────────────────────────────────────
#
# Backs the Cost-forecasting drill-in page. The forecast *card* on the
# Insights grid summarises this same projection; the detail page draws
# the line chart from the trailing daily-spend series, the month-end
# projection, and the active monthly tenant-budget caps to overlay.
# Money is serialised as plain JSON numbers (the frontend renders them
# with Intl.NumberFormat), so the Decimal totals are cast to float here.


class ForecastDailyPoint(BaseModel):
    """One UTC day of actual spend in the forecast series."""

    date: str  # YYYY-MM-DD
    usd: float


class ForecastBudget(BaseModel):
    """An active monthly tenant-scope budget cap, for the chart overlay."""

    name: str
    threshold_usd: float


class ForecastVendorProjection(BaseModel):
    """One vendor's month-to-date spend + projection (TM8 Phase D).

    Feeds the vendor-stacked forecast chart. ``basis`` is
    ``"estimated"`` (usage-token × rate card; Anthropic always, OpenAI
    when no billed data) or ``"authoritative"`` (OpenAI's billed
    ``/costs`` amounts) so the UI can label which is which.
    ``daily_series`` is this vendor's per-day actuals, so the chart can
    stack them and draw a per-vendor projection segment to
    ``projected_end``.
    """

    vendor: str  # "Anthropic" | "OpenAI"
    basis: str  # "estimated" | "authoritative"
    current_spend: float
    projected_end: float
    daily_series: list[ForecastDailyPoint]


class ForecastDetailResponse(BaseModel):
    """Spend projection + budget caps for the current UTC month.

    ``current_spend`` / ``projected_end`` / ``daily_series`` are the
    cross-vendor TOTALS (back-compat with the single-line chart). The
    TM8 ``vendors`` field carries the per-vendor breakdown for the
    vendor-stacked view — additive and optional: a pre-TM8 client
    ignores it, a single-vendor tenant gets a one-entry list.
    """

    period_start: str  # YYYY-MM-DD
    period_end: str  # YYYY-MM-DD
    current_spend: float
    projected_end: float
    days_remaining: int
    days_of_data: int
    daily_series: list[ForecastDailyPoint]
    budgets: list[ForecastBudget]
    vendors: list[ForecastVendorProjection] = []


@router.get(
    "/insights/forecast",
    response_model=ForecastDetailResponse,
    operation_id="getForecastDetail",
    tags=["insights"],
    summary="Current-month spend projection + budget caps",
)
def get_forecast_detail(
    user: AuthenticatedUser = Depends(current_user),
) -> ForecastDetailResponse:
    tenant_id = _require_tenant(user)

    # Anthropic baseline drives the period bounds + days_remaining (its
    # month arithmetic is the source of truth and is vendor-independent).
    res = project_period_end(tenant_id)

    # Per-vendor projections (Anthropic reuses ``res`` verbatim; OpenAI
    # is computed the same way). The top-level totals are the cross-vendor
    # sums so the existing single-line chart keeps working; the per-vendor
    # ``vendors`` list feeds the vendor-stacked view.
    forecasts = cost_forecasting.vendor_forecasts(tenant_id)

    total_current = sum(
        (vf.current_spend for vf in forecasts), Decimal("0")
    )
    total_projected = sum(
        (vf.projected_end for vf in forecasts), Decimal("0")
    )

    # Combined daily series — sum per UTC day across vendors, ascending.
    combined: dict[date, Decimal] = {}
    for vf in forecasts:
        for d, usd in vf.daily_series:
            combined[d] = combined.get(d, Decimal("0")) + usd
    combined_series = sorted(combined.items())
    # days_of_data = the most any single vendor has (matches the card).
    days_of_data = max(
        (len(vf.daily_series) for vf in forecasts), default=0
    )

    with session_scope(tenant_id) as s:
        rows = s.execute(
            sql_text(
                "SELECT name, threshold_usd FROM budgets "
                "WHERE deleted_at IS NULL "
                "AND scope_kind = 'tenant' AND period = 'monthly' "
                "ORDER BY threshold_usd ASC"
            )
        ).all()

    return ForecastDetailResponse(
        period_start=res.period_start.isoformat(),
        period_end=res.period_end.isoformat(),
        current_spend=float(total_current),
        projected_end=float(total_projected),
        days_remaining=res.days_remaining,
        days_of_data=days_of_data,
        daily_series=[
            ForecastDailyPoint(date=d.isoformat(), usd=float(usd))
            for (d, usd) in combined_series
        ],
        budgets=[
            ForecastBudget(
                name=r.name, threshold_usd=float(r.threshold_usd)
            )
            for r in rows
        ],
        vendors=[
            ForecastVendorProjection(
                vendor=vf.vendor,
                basis=vf.basis,
                current_spend=float(vf.current_spend),
                projected_end=float(vf.projected_end),
                daily_series=[
                    ForecastDailyPoint(date=d.isoformat(), usd=float(usd))
                    for (d, usd) in vf.daily_series
                ],
            )
            for vf in forecasts
        ],
    )


__all__ = ["router"]
