# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Budget-alert email template (TM3 Phase B4).

A budget alert fires when current-period spend crosses one of the
three thresholds (0.70 / 0.85 / 1.00). The evaluator inserts a row
into ``budget_alert_events`` with ON CONFLICT DO NOTHING; iff the
insert succeeded, it queues this email to every recipient on the
budget.

The body is plain English (no jargon) — the recipients may be
finance / ops people, not engineers. The dashboard link goes to
``/alerts`` so they can acknowledge in-app.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Optional, Union

from vargate_telemetry.notify.email import (
    EmailDeliveryError,
    SesNotConfigured,
    send_email,
)
from vargate_telemetry.notify.pagerduty import send_pagerduty_alert
from vargate_telemetry.notify.slack import send_slack_alert


_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BudgetAlertContext:
    """Everything the template needs.

    Built by the evaluator from the budgets row + computed spend.
    """

    budget_name: str
    scope_kind: str
    scope_label: str  # Human-readable scope description, e.g. "all of tnt_us_42" or "workspace=Engineering"
    period: str       # "daily" | "weekly" | "monthly"
    period_start: date
    period_end: date
    threshold_crossed: Decimal  # 0.70 / 0.85 / 1.00
    threshold_usd: Decimal
    current_spend_usd: Decimal
    # TM7 forecast alerts. ``kind`` selects the alert flavour:
    # "current_threshold" (default) = spend has already crossed the
    # threshold; "forecast_threshold" = spend is PROJECTED to reach it
    # by ``projected_breach_date`` on the current pace. Both fields are
    # defaulted so every existing call site is unaffected.
    kind: str = "current_threshold"
    projected_breach_date: Optional[date] = None


def _ratio_percent(t: Decimal) -> str:
    """0.70 → '70%'; 1.00 → '100%'."""
    return f"{int(t * 100)}%"


def _dashboard_url() -> str:
    """Where the email's CTA links to.

    Read from env so dev / staging / prod each link to the right
    surface. Defaults to the production host.
    """
    return os.environ.get(
        "OGMA_DASHBOARD_URL", "https://ogma.vargate.ai"
    ) + "/alerts"


def _breach_date_label(d: Optional[date]) -> str:
    """Render a projected-breach date like ``"Jun 24"`` (no leading zero).

    ``%-d`` is the glibc no-pad day directive (the codebase runs on
    Linux). Falls back to ``"month-end"`` when the date is missing — a
    forecast alert always carries one, but the template must never blow
    up on a None.
    """
    if d is None:
        return "month-end"
    return d.strftime("%b %-d")


def _render_forecast_alert(ctx: BudgetAlertContext) -> tuple[str, str, str]:
    """Build (subject, html_body, text_body) for a forecast alert.

    Mirrors the current-threshold template's branded "Ogma by Vargate"
    chrome + dashboard CTA, but the language says the threshold is
    PROJECTED to be reached on the current pace by ``projected_breach_date``
    — it has NOT been crossed yet.
    """
    pct = _ratio_percent(ctx.threshold_crossed)
    when = _breach_date_label(ctx.projected_breach_date)
    # No product prefix in the subject — same convention as the
    # current-threshold alert; the From + branded template carry identity.
    subject = (
        f"Forecast alert — \"{ctx.budget_name}\" projected to reach "
        f"{pct} of cap on {when}"
    )

    period_label = {
        "daily": "Today",
        "weekly": "This week",
        "monthly": "This month",
    }.get(ctx.period, ctx.period.capitalize())

    text_body = (
        f"Your budget \"{ctx.budget_name}\" is projected to reach {pct} of "
        f"its {ctx.period} cap on {when}, at the current pace of spend.\n\n"
        f"  Spend so far:   ${ctx.current_spend_usd}\n"
        f"  Threshold:      ${ctx.threshold_usd}\n"
        f"  Period:         {ctx.period_start} to {ctx.period_end}\n"
        f"  Scope:          {ctx.scope_label}\n\n"
        f"This is a projection, not a crossing — {period_label.lower()}'s "
        f"spend hasn't reached the threshold yet, but Ogma's forecast expects "
        f"it to by {when} if the current trend holds. View the alert and "
        f"acknowledge it in the dashboard:\n\n"
        f"  {_dashboard_url()}\n\n"
        f"-- \n"
        f"This alert is from Ogma by Vargate — your AI usage audit ledger.\n"
        f"You're receiving this because you're listed as a recipient on "
        f"the \"{ctx.budget_name}\" budget. Update recipients in\n"
        f"Ogma → Budgets.\n"
    )

    html_body = f"""<!DOCTYPE html>
<html>
<body style="margin: 0; padding: 0; background: #f4f4f3;
             font-family: -apple-system, system-ui, 'Segoe UI', sans-serif;">
  <div style="max-width: 560px; margin: 0 auto; background: #ffffff;
              border: 1px solid #e6e6e6; border-radius: 8px;
              overflow: hidden;">
    <div style="background: #1f1f1e; padding: 18px 24px;">
      <span style="color: #ffffff; font-size: 18px; font-weight: 700;
                   letter-spacing: -0.01em;">Ogma</span>
      <span style="color: #9a9a98; font-size: 13px;
                   margin-left: 8px;">by Vargate</span>
    </div>
    <div style="padding: 24px; font-size: 14px; color: #1f1f1e;">
      <h2 style="font-size: 16px; font-weight: 600; margin: 0 0 16px;">
        Forecast alert — &ldquo;{ctx.budget_name}&rdquo; projected to reach {pct} of cap on {when}
      </h2>
      <p style="margin: 0 0 16px;">
        Your budget &ldquo;<strong>{ctx.budget_name}</strong>&rdquo;
        is projected to reach <strong>{pct}</strong> of its {ctx.period} cap
        on <strong>{when}</strong>, at the current pace of spend. This is a
        projection &mdash; the threshold has not been crossed yet.
      </p>
      <table style="border-collapse: collapse; margin: 0 0 20px;">
        <tr>
          <td style="padding: 4px 12px 4px 0; color: #6b6b6b;">Spend so far</td>
          <td style="padding: 4px 0; font-family: ui-monospace, monospace;">
            ${ctx.current_spend_usd}
          </td>
        </tr>
        <tr>
          <td style="padding: 4px 12px 4px 0; color: #6b6b6b;">Threshold</td>
          <td style="padding: 4px 0; font-family: ui-monospace, monospace;">
            ${ctx.threshold_usd}
          </td>
        </tr>
        <tr>
          <td style="padding: 4px 12px 4px 0; color: #6b6b6b;">Projected to reach on</td>
          <td style="padding: 4px 0;">{when}</td>
        </tr>
        <tr>
          <td style="padding: 4px 12px 4px 0; color: #6b6b6b;">Period</td>
          <td style="padding: 4px 0;">
            {ctx.period_start} to {ctx.period_end}
          </td>
        </tr>
        <tr>
          <td style="padding: 4px 12px 4px 0; color: #6b6b6b;">Scope</td>
          <td style="padding: 4px 0;">{ctx.scope_label}</td>
        </tr>
      </table>
      <p style="margin: 0;">
        <a href="{_dashboard_url()}"
           style="display: inline-block; padding: 10px 18px;
                  background: #1f1f1e; color: #ffffff;
                  text-decoration: none; border-radius: 6px;
                  font-weight: 500;">
          View &amp; acknowledge in dashboard
        </a>
      </p>
    </div>
    <div style="padding: 16px 24px; border-top: 1px solid #e6e6e6;
                color: #6b6b6b; font-size: 12px; background: #fafafa;">
      This alert is from <strong>Ogma by Vargate</strong> — your AI usage
      audit ledger. You're receiving this because you're listed as a
      recipient on the &ldquo;{ctx.budget_name}&rdquo; budget.
      Update recipients in Ogma &rarr; Budgets.
    </div>
  </div>
</body>
</html>"""

    return subject, html_body, text_body


def render_budget_alert(ctx: BudgetAlertContext) -> tuple[str, str, str]:
    """Build (subject, html_body, text_body) for the given context.

    Pure function — no I/O. Tested separately from the SES call so
    template changes don't depend on AWS reachability.

    Dispatches on ``ctx.kind``: a ``forecast_threshold`` context renders
    the projection-language template; the default ``current_threshold``
    path below is byte-for-byte the original TM3 alert.
    """
    if ctx.kind == "forecast_threshold":
        return _render_forecast_alert(ctx)

    pct = _ratio_percent(ctx.threshold_crossed)
    # No product prefix in the subject — the From (Vargate.ai) and the
    # branded email template carry the identity. Keeping the subject
    # clean reads better in the inbox list.
    subject = f"Budget alert — \"{ctx.budget_name}\" at {pct} of cap"

    period_label = {
        "daily": "Today",
        "weekly": "This week",
        "monthly": "This month",
    }.get(ctx.period, ctx.period.capitalize())

    text_body = (
        f"Your budget \"{ctx.budget_name}\" has crossed {pct} of its "
        f"{ctx.period} cap.\n\n"
        f"  Current spend:  ${ctx.current_spend_usd}\n"
        f"  Threshold:      ${ctx.threshold_usd}\n"
        f"  Period:         {ctx.period_start} to {ctx.period_end}\n"
        f"  Scope:          {ctx.scope_label}\n\n"
        f"{period_label}'s spend is being attributed to this budget by "
        f"Ogma's evaluator. View the alert and acknowledge it in the "
        f"dashboard:\n\n"
        f"  {_dashboard_url()}\n\n"
        f"-- \n"
        f"This alert is from Ogma by Vargate — your AI usage audit ledger.\n"
        f"You're receiving this because you're listed as a recipient on "
        f"the \"{ctx.budget_name}\" budget. Update recipients in\n"
        f"Ogma → Budgets.\n"
    )

    # Branded but inbox-safe HTML — inline styles only, no remote
    # images or web fonts (the compliance-leaning audience's mail
    # clients strip those). A dark "Ogma by Vargate" header bar carries
    # the identity; the body stays a plain table + CTA. Colors match the
    # design-system ink/paper palette (#1f1f1e / #ffffff).
    html_body = f"""<!DOCTYPE html>
<html>
<body style="margin: 0; padding: 0; background: #f4f4f3;
             font-family: -apple-system, system-ui, 'Segoe UI', sans-serif;">
  <div style="max-width: 560px; margin: 0 auto; background: #ffffff;
              border: 1px solid #e6e6e6; border-radius: 8px;
              overflow: hidden;">
    <div style="background: #1f1f1e; padding: 18px 24px;">
      <span style="color: #ffffff; font-size: 18px; font-weight: 700;
                   letter-spacing: -0.01em;">Ogma</span>
      <span style="color: #9a9a98; font-size: 13px;
                   margin-left: 8px;">by Vargate</span>
    </div>
    <div style="padding: 24px; font-size: 14px; color: #1f1f1e;">
      <h2 style="font-size: 16px; font-weight: 600; margin: 0 0 16px;">
        Budget alert — &ldquo;{ctx.budget_name}&rdquo; at {pct} of cap
      </h2>
      <p style="margin: 0 0 16px;">
        Your budget &ldquo;<strong>{ctx.budget_name}</strong>&rdquo;
        has crossed <strong>{pct}</strong> of its {ctx.period} cap.
      </p>
      <table style="border-collapse: collapse; margin: 0 0 20px;">
        <tr>
          <td style="padding: 4px 12px 4px 0; color: #6b6b6b;">Current spend</td>
          <td style="padding: 4px 0; font-family: ui-monospace, monospace;">
            ${ctx.current_spend_usd}
          </td>
        </tr>
        <tr>
          <td style="padding: 4px 12px 4px 0; color: #6b6b6b;">Threshold</td>
          <td style="padding: 4px 0; font-family: ui-monospace, monospace;">
            ${ctx.threshold_usd}
          </td>
        </tr>
        <tr>
          <td style="padding: 4px 12px 4px 0; color: #6b6b6b;">Period</td>
          <td style="padding: 4px 0;">
            {ctx.period_start} to {ctx.period_end}
          </td>
        </tr>
        <tr>
          <td style="padding: 4px 12px 4px 0; color: #6b6b6b;">Scope</td>
          <td style="padding: 4px 0;">{ctx.scope_label}</td>
        </tr>
      </table>
      <p style="margin: 0;">
        <a href="{_dashboard_url()}"
           style="display: inline-block; padding: 10px 18px;
                  background: #1f1f1e; color: #ffffff;
                  text-decoration: none; border-radius: 6px;
                  font-weight: 500;">
          View &amp; acknowledge in dashboard
        </a>
      </p>
    </div>
    <div style="padding: 16px 24px; border-top: 1px solid #e6e6e6;
                color: #6b6b6b; font-size: 12px; background: #fafafa;">
      This alert is from <strong>Ogma by Vargate</strong> — your AI usage
      audit ledger. You're receiving this because you're listed as a
      recipient on the &ldquo;{ctx.budget_name}&rdquo; budget.
      Update recipients in Ogma &rarr; Budgets.
    </div>
  </div>
</body>
</html>"""

    return subject, html_body, text_body


# Per-channel recipient config (TM5 T5.4). The budget's
# `alert_recipients` is a JSONB object of this shape; email stays the
# default channel.
_CHANNELS = ("email", "slack_webhook", "pagerduty_key")


def _normalize_recipients(
    recipients: Union[dict[str, Any], list[str], None],
) -> dict[str, list[str]]:
    """Coerce the recipients arg into the per-channel dict.

    Accepts the per-channel config dict, OR a bare list of email
    addresses (back-compat for any legacy caller / test), OR None.
    """
    if isinstance(recipients, dict):
        return {ch: list(recipients.get(ch) or []) for ch in _CHANNELS}
    if recipients:  # a non-empty bare list → email-only
        return {"email": list(recipients), "slack_webhook": [], "pagerduty_key": []}
    return {ch: [] for ch in _CHANNELS}


def _send_email_channel(emails: list[str], ctx: BudgetAlertContext) -> dict[str, Any]:
    """Render + send the branded email to the address list.

    Catches the SES exceptions here (instead of bubbling to the
    evaluator) so a transient SES blip doesn't roll back the
    alert-event INSERT — the dashboard remains the source of truth.
    """
    subject, html_body, text_body = render_budget_alert(ctx)
    try:
        message_id = send_email(
            to=list(emails),
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )
        return {"status": "ok", "message_id": message_id, "count": len(emails)}
    except (SesNotConfigured, EmailDeliveryError) as exc:
        _log.warning(
            "send_budget_alert: email channel failed for budget %r: %s",
            ctx.budget_name,
            exc,
        )
        return {"status": "error", "detail": str(exc)}


def send_budget_alert(
    recipients: Union[dict[str, Any], list[str], None],
    ctx: BudgetAlertContext,
) -> dict[str, Any]:
    """Dispatch the budget alert over every configured channel.

    ``recipients`` is the per-channel config
    ``{"email": [...], "slack_webhook": [...], "pagerduty_key": [...]}``
    (a bare email list is accepted as email-only, back-compat).

    **Best-effort + isolated**: each channel's failure is caught,
    logged, and recorded in the returned summary — never raised. A
    notify failure must not roll back the evaluator's alert-event row
    (which would un-dedup the alert and re-fire next tick). Email is the
    default channel; Slack / PagerDuty fire only when configured.

    Returns a per-channel result summary (empty dict if no channel had
    recipients — a valid "alert recorded, nobody notified" state).
    """
    config = _normalize_recipients(recipients)
    summary: dict[str, Any] = {}

    if config["email"]:
        summary["email"] = _send_email_channel(config["email"], ctx)
    if config["slack_webhook"]:
        summary["slack"] = send_slack_alert(config["slack_webhook"], ctx)
    if config["pagerduty_key"]:
        summary["pagerduty"] = send_pagerduty_alert(config["pagerduty_key"], ctx)

    if not summary:
        _log.info(
            "send_budget_alert: budget %r has no recipients on any "
            "channel; skipping (alert row still recorded).",
            ctx.budget_name,
        )
    return summary
