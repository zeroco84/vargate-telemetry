# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Slack incoming-webhook alert channel (TM5 T5.4).

A budget can carry one or more Slack incoming-webhook URLs
(``https://hooks.slack.com/services/...``). On a threshold crossing we
POST a Block Kit message to each. The webhook URL *is* the secret — no
auth header — so we redact it in logs.

Best-effort: a failing webhook (bad URL, 404 from a revoked hook,
network blip) is logged and reported in the per-URL result list but
never raised — one channel must not block the others or roll back the
alert-event row. The HTTP call goes through ``_post`` so tests can
substitute it without monkeypatching httpx globally.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from vargate_telemetry.notify.budget_alert import BudgetAlertContext

_log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10.0
SLACK_WEBHOOK_PREFIX = "https://hooks.slack.com/"


def _redact(url: str) -> str:
    """A log-safe form of a webhook URL (the path segments are the
    secret). Keep the host + the first path segment only."""
    try:
        rest = url.split("://", 1)[1]
        host = rest.split("/", 1)[0]
        return f"{host}/…"
    except Exception:
        return "hooks.slack.com/…"


def _post(url: str, payload: dict[str, Any]) -> httpx.Response:
    """One webhook POST. Isolated so tests can replace it."""
    return httpx.post(url, json=payload, timeout=_TIMEOUT_SECONDS)


def render_slack_alert(ctx: "BudgetAlertContext") -> dict[str, Any]:
    """Build the Slack Block Kit payload. Pure (no I/O).

    ``text`` is the notification fallback (shown in the sidebar / push);
    ``blocks`` is the rich in-channel rendering.
    """
    pct = f"{int(ctx.threshold_crossed * 100)}%"
    text = (
        f'Budget alert: "{ctx.budget_name}" at {pct} of cap '
        f"(${ctx.current_spend_usd} / ${ctx.threshold_usd})"
    )
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Budget alert — {pct} of cap"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Budget:*\n{ctx.budget_name}"},
                {"type": "mrkdwn", "text": f"*Scope:*\n{ctx.scope_label}"},
                {"type": "mrkdwn", "text": f"*Spend:*\n${ctx.current_spend_usd}"},
                {"type": "mrkdwn", "text": f"*Threshold:*\n${ctx.threshold_usd}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Period:*\n{ctx.period_start} → {ctx.period_end}",
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "Ogma by Vargate — AI usage audit ledger"},
            ],
        },
    ]
    return {"text": text, "blocks": blocks}


def send_slack_alert(
    webhook_urls: list[str], ctx: "BudgetAlertContext"
) -> list[dict[str, Any]]:
    """POST the alert to each webhook. Best-effort, never raises.

    Returns one ``{"target": <redacted url>, "status": "ok"|"error",
    "detail"?: str}`` per URL.
    """
    payload = render_slack_alert(ctx)
    results: list[dict[str, Any]] = []
    for url in webhook_urls:
        target = _redact(url)
        try:
            resp = _post(url, payload)
            resp.raise_for_status()
            results.append({"target": target, "status": "ok"})
        except Exception as exc:  # noqa: BLE001 — best-effort channel
            _log.warning(
                "send_slack_alert: POST to %s failed for budget %r: %s",
                target,
                ctx.budget_name,
                exc,
            )
            results.append(
                {"target": target, "status": "error", "detail": str(exc)}
            )
    return results
