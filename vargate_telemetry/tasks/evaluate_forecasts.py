# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Forecast budget-alert Celery task (TM7).

Companion to ``evaluate_budgets`` (the current-threshold evaluator).
Where that task fires when spend has *already* crossed a threshold,
this one fires when month-to-date spend is *projected* to reach a
threshold by month-end on the current pace — an early warning a day
or week before the cap is actually hit.

Architecture
============

Same two-task shape as ``evaluate_budgets``:

1. :func:`dispatch_evaluate_forecasts` (beat task)
   - Runs under ``scheduler_session_scope`` (cross-tenant view) to
     enumerate ``tenants WHERE active = true`` (or in a given region).
   - Queues ``evaluate_forecasts_for_tenant.delay(t)`` per tenant.

2. :func:`evaluate_forecasts_for_tenant` (per-tenant task)
   - Opens ``session_scope(tenant_id)`` so RLS gates every read and
     write to the tenant's own budgets + alert events.
   - Computes the month-to-date forecast once per tenant via
     :func:`project_period_end`.
   - Only tenant-wide monthly budgets are forecast (the forecast is a
     month-end projection of *tenant* spend — scoped / non-monthly
     budgets don't have a comparable month-end number).

Guardrails
==========

The forecast is only trustworthy with enough data and a positive
trend:

- ``days_of_data < 7`` → too little history; skip (no alerts).
- ``slope_per_day <= 0`` → flat or shrinking spend; the projection
  never crosses anything new, so there's nothing to warn about. Skip.

Idempotency
===========

Dedup mirrors ``evaluate_budgets`` but on the widened 4-column key
``(budget_id, period_start, threshold_crossed, kind)`` (migration
0024). ``kind = 'forecast_threshold'`` keeps forecast alerts in their
own dedup lane, distinct from the ``current_threshold`` crossings — a
budget can fire a forecast alert AND, later in the period, the actual
crossing, and the customer sees both. A second tick that re-projects
the same threshold re-runs the INSERT, the ON CONFLICT fires, no row,
no email.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import text as sql_text

from vargate_telemetry.budgets import ALERT_THRESHOLDS
from vargate_telemetry.celery_app import celery_app
from vargate_telemetry.db import scheduler_session_scope, session_scope
from vargate_telemetry.insights.spend_data import project_period_end
from vargate_telemetry.notify import (
    BudgetAlertContext,
    send_budget_alert,
)


_log = logging.getLogger(__name__)


@celery_app.task(
    name=(
        "vargate_telemetry.tasks.evaluate_forecasts."
        "evaluate_forecasts_for_tenant"
    ),
)
def evaluate_forecasts_for_tenant(tenant_id: str) -> dict:
    """Project month-end spend; fire forecast alerts as needed.

    Returns a structured dict so the beat-log shows what happened on
    each tick: how many monthly tenant budgets were checked, whether
    the forecast was usable, and which (budget, threshold) projections
    newly fired.
    """
    budgets_checked = 0
    thresholds_fired: list[str] = []  # "<budget_id>:<threshold>"

    # Month-to-date forecast for this tenant. Never raises (it degrades
    # to a flat, zero-slope projection on any internal error), so the
    # guardrails below also cover the failure case.
    res = project_period_end(tenant_id)

    # Not enough history, or a flat / shrinking trend → nothing to warn
    # about. Bail before touching the DB so an idle tenant is a no-op.
    if res.days_of_data < 7 or res.slope_per_day <= 0:
        return {
            "tenant_id": tenant_id,
            "budgets_checked": 0,
            "thresholds_fired": [],
            "skipped": "insufficient_data_or_flat_trend",
        }

    today = datetime.now(timezone.utc).date()
    slope = Decimal(str(res.slope_per_day))

    with session_scope(tenant_id) as s:
        rows = list(
            s.execute(
                sql_text(
                    """
                    SELECT id::text AS id, name, threshold_usd,
                           alert_recipients
                    FROM budgets
                    WHERE deleted_at IS NULL
                      AND scope_kind = 'tenant'
                      AND period = 'monthly'
                    """
                )
            )
        )
        for row in rows:
            budgets_checked += 1
            try:
                threshold_usd = Decimal(row.threshold_usd)
                for threshold in ALERT_THRESHOLDS:
                    target = threshold * threshold_usd
                    if res.projected_end < target:
                        continue

                    # Days until the projected spend reaches the target,
                    # at the current per-day pace. Clamp the lower bound
                    # to 0 (the target may already be passed by current
                    # spend) and the upper bound to month-end (the
                    # projection's horizon — never point past it).
                    days_ahead = (target - res.current_spend) / slope
                    if days_ahead < 0:
                        days_ahead = Decimal("0")
                    breach_date = today + timedelta(days=float(days_ahead))
                    if breach_date > res.period_end:
                        breach_date = res.period_end

                    # ON CONFLICT DO NOTHING on the widened 4-col dedup
                    # key — forecast alerts live in kind='forecast_threshold'
                    # so they don't collide with current_threshold rows.
                    # RETURNING is nonempty iff this INSERT wrote a row.
                    inserted = s.execute(
                        sql_text(
                            """
                            INSERT INTO budget_alert_events (
                                budget_id, tenant_id, period_start,
                                threshold_crossed, current_spend_usd, kind
                            )
                            VALUES (
                                :budget_id,
                                current_setting('app.tenant_id'),
                                :period_start,
                                :threshold,
                                :spend,
                                'forecast_threshold'
                            )
                            ON CONFLICT (
                                budget_id, period_start,
                                threshold_crossed, kind
                            )
                            DO NOTHING
                            RETURNING id::text
                            """
                        ),
                        {
                            "budget_id": row.id,
                            "period_start": res.period_start,
                            "threshold": threshold,
                            "spend": res.current_spend,
                        },
                    ).first()
                    if inserted is None:
                        continue  # already fired this period
                    thresholds_fired.append(f"{row.id}:{threshold}")

                    # Multi-channel dispatch is best-effort + isolated
                    # inside send_budget_alert — a notify failure NEVER
                    # rolls back the alert row (the dashboard is the
                    # source of truth). It returns a per-channel summary.
                    summary = send_budget_alert(
                        recipients=row.alert_recipients or {},
                        ctx=BudgetAlertContext(
                            budget_name=row.name,
                            scope_kind="tenant",
                            scope_label="entire tenant",
                            period="monthly",
                            period_start=res.period_start,
                            period_end=res.period_end,
                            threshold_crossed=threshold,
                            threshold_usd=threshold_usd,
                            current_spend_usd=res.current_spend,
                            kind="forecast_threshold",
                            projected_breach_date=breach_date,
                        ),
                    )
                    _log.info(
                        "forecast alert dispatched for %s (%s) at "
                        "threshold %s, projected %s: %s",
                        row.id,
                        row.name,
                        threshold,
                        breach_date,
                        summary,
                    )
            except Exception:  # noqa: BLE001
                # One budget's evaluator should not poison the rest —
                # log + continue, same posture as evaluate_budgets.
                _log.exception(
                    "evaluate_forecasts_for_tenant: budget %s failed; "
                    "continuing with the next budget for tenant %s",
                    row.id,
                    tenant_id,
                )

    return {
        "tenant_id": tenant_id,
        "budgets_checked": budgets_checked,
        "thresholds_fired": thresholds_fired,
    }


@celery_app.task(
    name=(
        "vargate_telemetry.tasks.evaluate_forecasts."
        "dispatch_evaluate_forecasts"
    ),
)
def dispatch_evaluate_forecasts(region: Optional[str] = None) -> int:
    """Beat fan-out. Enumerate active tenants; queue one eval per tenant.

    Mirrors ``dispatch_evaluate_budgets`` — same role + same query
    shape + same region semantics. Returns the dispatched count for the
    beat log.
    """
    # TM5 T5.0 region semantics: default dispatches all active tenants
    # (region=None means "every region"); an explicit region= filters.
    with scheduler_session_scope() as s:
        if region is None:
            rows = s.execute(
                sql_text(
                    "SELECT tenant_id FROM tenants WHERE active = true"
                )
            ).all()
        else:
            rows = s.execute(
                sql_text(
                    "SELECT tenant_id FROM tenants "
                    "WHERE active = true AND region = :r"
                ),
                {"r": region},
            ).all()

    for row in rows:
        evaluate_forecasts_for_tenant.delay(row.tenant_id)

    _log.info(
        "dispatch_evaluate_forecasts: queued %d tenants in region %s",
        len(rows),
        region or "all",
    )
    return len(rows)
