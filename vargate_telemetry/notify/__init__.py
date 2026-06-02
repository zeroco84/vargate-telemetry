# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Outbound notifications.

Channels: email (AWS SES, the default), Slack incoming webhooks, and
PagerDuty Events API v2 — added TM5 T5.4. ``send_budget_alert``
dispatches a budget alert over whichever channels a budget configures
(its `alert_recipients` JSONB), best-effort + isolated per channel.

Sender identity + IAM credentials + SES sandbox status live in
``docs/ops/integrations/aws-ses.md``; the multi-channel config + how to
get a Slack webhook / PagerDuty routing key live in
``docs/ops/integrations/alert-channels.md``.
"""

from vargate_telemetry.notify.email import (
    EmailDeliveryError,
    SesNotConfigured,
    send_email,
)
from vargate_telemetry.notify.budget_alert import (
    BudgetAlertContext,
    render_budget_alert,
    send_budget_alert,
)
from vargate_telemetry.notify.pagerduty import (
    render_pagerduty_event,
    send_pagerduty_alert,
)
from vargate_telemetry.notify.slack import (
    render_slack_alert,
    send_slack_alert,
)

__all__ = [
    "BudgetAlertContext",
    "EmailDeliveryError",
    "SesNotConfigured",
    "render_budget_alert",
    "render_pagerduty_event",
    "render_slack_alert",
    "send_budget_alert",
    "send_email",
    "send_pagerduty_alert",
    "send_slack_alert",
]
