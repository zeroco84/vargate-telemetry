# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""PagerDuty Events API v2 alert channel (TM5 T5.4).

A budget can carry one or more PagerDuty Events API v2 **routing keys**
(a.k.a. integration keys — 32-char). On a threshold crossing we POST a
``trigger`` event to ``https://events.pagerduty.com/v2/enqueue`` for
each. Severity is ``critical`` at 100% of cap, ``warning`` below.

The ``dedup_key`` is stable per (budget, period, threshold) so a retry
of the same crossing collapses into one PagerDuty incident — defence in
depth on top of the evaluator's own once-per-threshold-per-period dedup.

Best-effort: a failing key (revoked integration, network blip) is logged
+ reported but never raised. The HTTP call goes through ``_post`` so
tests can substitute it.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from vargate_telemetry.notify.budget_alert import BudgetAlertContext

_log = logging.getLogger(__name__)

PAGERDUTY_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"
_TIMEOUT_SECONDS = 10.0


def _redact(routing_key: str) -> str:
    """Log-safe form of a routing key — last 4 chars only."""
    return f"…{routing_key[-4:]}" if len(routing_key) >= 4 else "…"


def _severity(threshold_crossed: Decimal) -> str:
    return "critical" if threshold_crossed >= Decimal("1.00") else "warning"


def _post(payload: dict[str, Any]) -> httpx.Response:
    """One Events API POST. Isolated so tests can replace it."""
    return httpx.post(
        PAGERDUTY_EVENTS_URL, json=payload, timeout=_TIMEOUT_SECONDS
    )


def render_pagerduty_event(
    routing_key: str, ctx: "BudgetAlertContext"
) -> dict[str, Any]:
    """Build the Events API v2 ``trigger`` payload. Pure (no I/O)."""
    pct = f"{int(ctx.threshold_crossed * 100)}%"
    return {
        "routing_key": routing_key,
        "event_action": "trigger",
        # Stable per crossing -> one incident even if re-sent.
        "dedup_key": (
            f"ogma-budget-{ctx.budget_name}-"
            f"{ctx.period_start.isoformat()}-{ctx.threshold_crossed}"
        ),
        "payload": {
            "summary": (
                f'Budget "{ctx.budget_name}" at {pct} of cap '
                f"(${ctx.current_spend_usd} / ${ctx.threshold_usd})"
            ),
            "source": "ogma.vargate.ai",
            "severity": _severity(ctx.threshold_crossed),
            "component": ctx.scope_label,
            "group": ctx.period,
            "custom_details": {
                "budget": ctx.budget_name,
                "scope": ctx.scope_label,
                "period": f"{ctx.period_start} to {ctx.period_end}",
                "current_spend_usd": str(ctx.current_spend_usd),
                "threshold_usd": str(ctx.threshold_usd),
                "threshold_crossed": str(ctx.threshold_crossed),
            },
        },
    }


def send_pagerduty_alert(
    routing_keys: list[str], ctx: "BudgetAlertContext"
) -> list[dict[str, Any]]:
    """POST a trigger event for each routing key. Best-effort, never raises.

    Returns one ``{"target": <redacted key>, "status": "ok"|"error",
    "detail"?: str}`` per key.
    """
    results: list[dict[str, Any]] = []
    for key in routing_keys:
        target = _redact(key)
        try:
            resp = _post(render_pagerduty_event(key, ctx))
            resp.raise_for_status()  # Events API returns 202 on accept
            results.append({"target": target, "status": "ok"})
        except Exception as exc:  # noqa: BLE001 — best-effort channel
            _log.warning(
                "send_pagerduty_alert: enqueue for key %s failed for "
                "budget %r: %s",
                target,
                ctx.budget_name,
                exc,
            )
            results.append(
                {"target": target, "status": "error", "detail": str(exc)}
            )
    return results
