# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Budget alert channels (TM5 T5.4): alert_recipients text[] -> JSONB.

Multi-channel alerts: a budget can notify over email (default), Slack
incoming webhooks, and/or PagerDuty Events API routing keys. The
``budgets.alert_recipients`` column changes from a flat ``varchar[]`` of
email addresses to a per-channel JSONB object::

    {"email": [...], "slack_webhook": [...], "pagerduty_key": [...]}

The upgrade wraps every existing email list under the ``email`` key, so
no recipient is lost and email keeps firing exactly as before. The
column stays ``NOT NULL`` with an empty-all-channels default.

Down-migration extracts the ``email`` array back to ``varchar[]`` —
Slack/PagerDuty recipients are dropped on downgrade (the old column
can't hold them), which is the expected, lossy-by-design reverse.
"""

from typing import Union

from alembic import op

revision: str = "0023_budget_alert_channels"
down_revision: Union[str, None] = "0022_add_user_role"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


_EMPTY_CONFIG = '{"email": [], "slack_webhook": [], "pagerduty_key": []}'


def upgrade() -> None:
    # Drop the old varchar[] default before the type change (the default
    # expression is type-incompatible with jsonb).
    op.execute("ALTER TABLE budgets ALTER COLUMN alert_recipients DROP DEFAULT")
    # Convert in place: existing emails -> {"email": [...], slack: [], pd: []}.
    op.execute(
        """
        ALTER TABLE budgets
        ALTER COLUMN alert_recipients TYPE JSONB
        USING jsonb_build_object(
            'email', to_jsonb(COALESCE(alert_recipients, ARRAY[]::varchar[])),
            'slack_webhook', '[]'::jsonb,
            'pagerduty_key', '[]'::jsonb
        )
        """
    )
    op.execute(
        f"ALTER TABLE budgets ALTER COLUMN alert_recipients "
        f"SET DEFAULT '{_EMPTY_CONFIG}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE budgets ALTER COLUMN alert_recipients DROP DEFAULT")
    # Extract the email array back to varchar[]; Slack/PagerDuty recipients
    # are dropped (the old column can't represent them).
    op.execute(
        """
        ALTER TABLE budgets
        ALTER COLUMN alert_recipients TYPE varchar(320)[]
        USING ARRAY(
            SELECT jsonb_array_elements_text(
                COALESCE(alert_recipients->'email', '[]'::jsonb)
            )
        )::varchar(320)[]
        """
    )
    op.execute(
        "ALTER TABLE budgets ALTER COLUMN alert_recipients "
        "SET DEFAULT ARRAY[]::varchar[]"
    )
