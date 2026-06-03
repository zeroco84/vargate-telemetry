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

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text as sql_text

from vargate_telemetry.auth.middleware import (
    AuthenticatedUser,
    current_user,
)
from vargate_telemetry.db import session_scope
from vargate_telemetry.insights.aggregator import build_insights
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


class ForecastDetailResponse(BaseModel):
    """Spend projection + budget caps for the current UTC month."""

    period_start: str  # YYYY-MM-DD
    period_end: str  # YYYY-MM-DD
    current_spend: float
    projected_end: float
    days_remaining: int
    days_of_data: int
    daily_series: list[ForecastDailyPoint]
    budgets: list[ForecastBudget]


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
    res = project_period_end(tenant_id)

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
        current_spend=float(res.current_spend),
        projected_end=float(res.projected_end),
        days_remaining=res.days_remaining,
        days_of_data=res.days_of_data,
        daily_series=[
            ForecastDailyPoint(date=d.isoformat(), usd=float(usd))
            for (d, usd) in res.daily_series
        ],
        budgets=[
            ForecastBudget(
                name=r.name, threshold_usd=float(r.threshold_usd)
            )
            for r in rows
        ],
    )


__all__ = ["router"]
