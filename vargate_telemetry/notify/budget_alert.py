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
from typing import Optional

from vargate_telemetry.notify.email import (
    EmailDeliveryError,
    SesNotConfigured,
    send_email,
)


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


def render_budget_alert(ctx: BudgetAlertContext) -> tuple[str, str, str]:
    """Build (subject, html_body, text_body) for the given context.

    Pure function — no I/O. Tested separately from the SES call so
    template changes don't depend on AWS reachability.
    """
    pct = _ratio_percent(ctx.threshold_crossed)
    subject = (
        f"[Ogma] Budget alert — \"{ctx.budget_name}\" at {pct} of cap"
    )

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
        f"This alert is from Ogma — your AI usage audit ledger.\n"
        f"You're receiving this because you're listed as a recipient on "
        f"the \"{ctx.budget_name}\" budget. Update recipients in\n"
        f"Ogma → Budgets.\n"
    )

    # Minimal-HTML body — no inline images, no remote fonts. Plain
    # table + link. Compliance-friendly inboxes (mostly the audience
    # here) tend to strip rich HTML; we lose nothing by staying simple.
    html_body = f"""<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, system-ui, sans-serif;
             font-size: 14px; color: #1c1c1c; max-width: 560px;">
  <h2 style="font-size: 16px; font-weight: 600; margin: 0 0 16px;">
    Budget alert — &ldquo;{ctx.budget_name}&rdquo; at {pct} of cap
  </h2>
  <p>
    Your budget &ldquo;<strong>{ctx.budget_name}</strong>&rdquo;
    has crossed <strong>{pct}</strong> of its {ctx.period} cap.
  </p>
  <table style="border-collapse: collapse; margin: 16px 0;">
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
  <p>
    <a href="{_dashboard_url()}"
       style="display: inline-block; padding: 8px 16px;
              background: #1c1c1c; color: #fff;
              text-decoration: none; border-radius: 4px;">
      View &amp; acknowledge in dashboard
    </a>
  </p>
  <hr style="border: 0; border-top: 1px solid #e6e6e6; margin: 24px 0;">
  <p style="color: #6b6b6b; font-size: 12px;">
    This alert is from Ogma — your AI usage audit ledger.
    You're receiving this because you're listed as a recipient
    on the &ldquo;{ctx.budget_name}&rdquo; budget.
    Update recipients in Ogma → Budgets.
  </p>
</body>
</html>"""

    return subject, html_body, text_body


def send_budget_alert(
    recipients: list[str], ctx: BudgetAlertContext
) -> Optional[str]:
    """Format + send the budget-alert email.

    Returns the SES MessageId on success, ``None`` if recipients is
    empty (treated as a no-op rather than an error — a budget with
    no recipients is a valid configuration that the customer may
    iterate toward).

    Raises ``SesNotConfigured`` or ``EmailDeliveryError`` — the
    evaluator catches both and logs; we don't want a transient SES
    blip to roll back the alert-event INSERT (which would un-dedup
    the alert and re-fire on the next 15-minute tick).
    """
    if not recipients:
        _log.info(
            "send_budget_alert: budget %r has no recipients; "
            "skipping (alert row still recorded).",
            ctx.budget_name,
        )
        return None

    subject, html_body, text_body = render_budget_alert(ctx)
    try:
        return send_email(
            to=list(recipients),
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )
    except (SesNotConfigured, EmailDeliveryError):
        # Re-raise so the evaluator can log structurally. We don't
        # swallow at this layer because there's a real difference
        # between "delivery failed" and "we chose not to send".
        raise
