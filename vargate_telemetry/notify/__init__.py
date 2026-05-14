# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Outbound notifications (TM3 Phase B4).

Today this is just email (AWS SES). Slack / SMS / PagerDuty are
listed as TM4-or-later in the TM3 spec; the directory exists so
those land as sibling modules without a refactor.

Sender identity + IAM credentials + SES sandbox status live in
``docs/ops/integrations/aws-ses.md`` — see that doc before adding
a new alert template.
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

__all__ = [
    "BudgetAlertContext",
    "EmailDeliveryError",
    "SesNotConfigured",
    "render_budget_alert",
    "send_budget_alert",
    "send_email",
]
