# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Add `budget_alert_events.kind` + widen the dedup constraint (TM7).

Two flavours of budget alert now share the ``budget_alert_events``
table:

- ``current_threshold`` — the existing TM3 alert: current-period spend
  has actually crossed 70 / 85 / 100 % of the cap.
- ``forecast_threshold`` — the new TM7 alert: spend is PROJECTED to
  reach a threshold by month-end on the current pace, even though it
  hasn't crossed yet.

Both kinds want to fire at most once per (budget, period, threshold),
but a forecast alert and a current alert for the same threshold are
distinct events the customer should see independently. So ``kind``
joins the dedup key: the unique constraint widens from
``(budget_id, period_start, threshold_crossed)`` to
``(budget_id, period_start, threshold_crossed, kind)``.

The column is ``NOT NULL`` with a ``server_default`` of
``'current_threshold'`` so every pre-existing row (all of which are
current-threshold crossings) backfills correctly and the existing
evaluator's INSERT keeps working unchanged until it's updated to set
``kind`` explicitly. The default is intentionally KEPT (not dropped
after backfill) so the column stays safe for any code path that
omits it.

Revision ID: 0024_budget_alert_kind
Revises: 0023_budget_alert_channels
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_budget_alert_kind"
down_revision: Union[str, None] = "0023_budget_alert_channels"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "budget_alert_events",
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            server_default="current_threshold",
        ),
    )
    op.drop_constraint(
        "uq_budget_alert_events_dedup",
        "budget_alert_events",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_budget_alert_events_dedup",
        "budget_alert_events",
        ["budget_id", "period_start", "threshold_crossed", "kind"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_budget_alert_events_dedup",
        "budget_alert_events",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_budget_alert_events_dedup",
        "budget_alert_events",
        ["budget_id", "period_start", "threshold_crossed"],
    )
    op.drop_column("budget_alert_events", "kind")
