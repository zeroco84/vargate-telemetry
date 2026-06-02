# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Budget evaluation Celery task (TM3 Phase B3).

Beat fan-out + per-tenant evaluator. Runs every 15 minutes (same
cadence as the admin / code-analytics / activity-feed pulls — the
fresh spend numbers feed straight into the threshold comparison so
crossing the cap shows up in the dashboard within one tick).

Architecture
============

Two tasks, mirroring the ``pull_admin`` dispatcher pattern:

1. :func:`dispatch_evaluate_budgets` (beat task)
   - Runs under ``scheduler_session_scope`` (cross-tenant view) to
     enumerate ``tenants WHERE active = true``.
   - For each tenant, queues ``evaluate_budgets_for_tenant.delay(t)``.

2. :func:`evaluate_budgets_for_tenant` (per-tenant task)
   - Opens ``session_scope(tenant_id)`` so RLS gates every read and
     write to the tenant's own budgets + alert events.
   - SELECTs live budgets for the tenant.
   - For each budget: computes the current period's window, the
     spend in that window, and the ratio against the threshold.
   - For each of the three thresholds (0.70 / 0.85 / 1.00) the
     ratio meets-or-exceeds: INSERT a row into
     ``budget_alert_events`` with ON CONFLICT DO NOTHING. The
     unique constraint
     ``(budget_id, period_start, threshold_crossed)`` means each
     threshold per (budget, period) is at-most-once-firing.
   - When an INSERT succeeded (the RETURNING clause produced a row),
     queue the corresponding budget-alert email to every recipient.
     Email failures DO NOT roll back the INSERT — the alert event
     row is the source of truth; an SES blip just means the human
     learns about it from the dashboard instead of the inbox.

Idempotency
===========

A second tick within the same period sees ratio still >= 0.70,
runs the INSERT again, the ON CONFLICT fires, no row is inserted,
no email is sent. Customers don't get spammed.

If the customer raises the threshold mid-period (e.g. 100 → 200,
new ratio = 0.30), nothing fires until the new period opens AND
the new ratio crosses the new thresholds. That's the correct
behaviour — the budget changed, the alert math changed with it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import text as sql_text

from vargate_telemetry.budgets import (
    ALERT_THRESHOLDS,
    compute_spend_in_window,
    current_period_window,
)
from vargate_telemetry.celery_app import celery_app
from vargate_telemetry.db import scheduler_session_scope, session_scope
from vargate_telemetry.notify import (
    BudgetAlertContext,
    send_budget_alert,
)


_log = logging.getLogger(__name__)


def _scope_label(scope_kind: str, scope_value: Optional[str]) -> str:
    """Build a human-readable scope label for the alert email.

    The email recipients aren't necessarily engineers; "scope:
    workspace = wrkspc_01HABC..." reads worse than "scope: workspace
    Engineering". For now we surface the raw scope_value — looking
    up the friendly name from workspaces / api_keys side tables is
    a polish item for TM4 (the dashboard already resolves these via
    LEFT JOINs).
    """
    if scope_kind == "tenant":
        return "All Anthropic API usage for this tenant"
    return f"{scope_kind} = {scope_value}"


def _evaluate_one_budget(
    session,  # type: ignore[no-untyped-def]
    *,
    budget_id: str,
    name: str,
    scope_kind: str,
    scope_value: Optional[str],
    period: str,
    threshold_usd: Decimal,
    recipients: dict,  # per-channel JSONB: {email, slack_webhook, pagerduty_key}
    now: Optional[datetime] = None,
) -> list[Decimal]:
    """Evaluate one budget. Returns the thresholds that newly fired.

    The caller has already opened ``session_scope(tenant_id)``.
    ``now`` is injectable for tests (default = wall clock UTC).
    """
    window = current_period_window(period, now=now)
    spend = compute_spend_in_window(
        session,
        start=window.start,
        end=window.end,
        scope_kind=scope_kind,
        scope_value=scope_value,
    )

    # Guard against threshold_usd == 0 (CHECK constraint forbids it
    # but defense in depth — would cause ZeroDivisionError otherwise).
    if threshold_usd <= 0:
        return []  # pragma: no cover
    ratio = spend / threshold_usd

    newly_fired: list[Decimal] = []
    for threshold in ALERT_THRESHOLDS:
        if ratio < threshold:
            continue
        # ON CONFLICT DO NOTHING on (budget_id, period_start,
        # threshold_crossed) — first crossing wins; re-evaluations
        # within the same period are silent no-ops. RETURNING is
        # nonempty iff this INSERT actually wrote a row.
        result = session.execute(
            sql_text(
                """
                INSERT INTO budget_alert_events (
                    budget_id, tenant_id, period_start,
                    threshold_crossed, current_spend_usd
                )
                VALUES (
                    :budget_id,
                    current_setting('app.tenant_id'),
                    :period_start,
                    :threshold,
                    :spend
                )
                ON CONFLICT (budget_id, period_start, threshold_crossed)
                DO NOTHING
                RETURNING id::text
                """
            ),
            {
                "budget_id": budget_id,
                "period_start": window.start_date,
                "threshold": threshold,
                "spend": spend,
            },
        ).first()
        if result is None:
            continue  # already fired this period
        newly_fired.append(threshold)

        # Multi-channel dispatch (email / Slack / PagerDuty) is
        # best-effort + isolated inside send_budget_alert — a notify
        # failure NEVER rolls back the alert row (the dashboard is the
        # source of truth). It returns a per-channel summary; we log it.
        summary = send_budget_alert(
            recipients=recipients,
            ctx=BudgetAlertContext(
                budget_name=name,
                scope_kind=scope_kind,
                scope_label=_scope_label(scope_kind, scope_value),
                period=period,
                period_start=window.start_date,
                period_end=window.end.date(),
                threshold_crossed=threshold,
                threshold_usd=threshold_usd,
                current_spend_usd=spend,
            ),
        )
        _log.info(
            "budget alert dispatched for %s (%s) at threshold %s: %s",
            budget_id,
            name,
            threshold,
            summary,
        )

    return newly_fired


@celery_app.task(
    name=(
        "vargate_telemetry.tasks.evaluate_budgets."
        "evaluate_budgets_for_tenant"
    ),
)
def evaluate_budgets_for_tenant(tenant_id: str) -> dict:
    """Walk every live budget for the tenant; fire alerts as needed.

    Returns a structured dict so the beat-log shows what happened
    on each tick: how many budgets were checked, how many newly-
    crossed thresholds fired.
    """
    budgets_checked = 0
    thresholds_fired: list[str] = []  # "<budget_id>:<threshold>"

    with session_scope(tenant_id) as s:
        result = s.execute(
            sql_text(
                """
                SELECT id::text AS id, name, scope_kind, scope_value,
                       period, threshold_usd, alert_recipients
                FROM budgets
                WHERE deleted_at IS NULL
                """
            )
        )
        rows = list(result)
        for row in rows:
            budgets_checked += 1
            try:
                fired = _evaluate_one_budget(
                    s,
                    budget_id=row.id,
                    name=row.name,
                    scope_kind=row.scope_kind,
                    scope_value=row.scope_value,
                    period=row.period,
                    threshold_usd=Decimal(row.threshold_usd),
                    recipients=row.alert_recipients or {},
                )
                for t in fired:
                    thresholds_fired.append(f"{row.id}:{t}")
            except Exception:  # noqa: BLE001
                # One budget's evaluator should not poison the rest.
                # Most likely a transient DB hiccup or a bad spend
                # SQL parameter — log + continue.
                _log.exception(
                    "evaluate_budgets_for_tenant: budget %s failed; "
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
        "vargate_telemetry.tasks.evaluate_budgets."
        "dispatch_evaluate_budgets"
    ),
)
def dispatch_evaluate_budgets(region: Optional[str] = None) -> int:
    """Beat fan-out. Enumerate active tenants; queue one eval per tenant.

    Mirrors ``dispatch_admin_pulls`` — same role + same query shape.
    Returns the dispatched count for the beat log.
    """
    # TM5 T5.0: default dispatches all active tenants; the region gap (defaulting to VARGATE_REGION=us) silently skipped eu tenants. region arg kept as an explicit override.
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
        evaluate_budgets_for_tenant.delay(row.tenant_id)

    _log.info(
        "dispatch_evaluate_budgets: queued %d tenants in region %s",
        len(rows),
        region or "all",
    )
    return len(rows)
