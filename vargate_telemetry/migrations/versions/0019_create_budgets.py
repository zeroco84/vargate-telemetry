# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Add `budgets` + `budget_alert_events` tables (TM3 Phase B1).

Ogma's CFO-facing analytic that Anthropic's console doesn't address:
spend caps per (api_key | workspace | model | tenant), evaluated on
a 15-minute cadence, with email alerts at 70 / 85 / 100 % of the
threshold. Three independent thresholds per (budget, period) — each
fires AT MOST once per (budget_id, period_start, threshold_crossed),
enforced by the UNIQUE constraint below + ON CONFLICT DO NOTHING in
the evaluator (TM3 §2.3).

Schema decisions
================

- `tenant_id` denormalized on `budget_alert_events` so the RLS
  policy can stay simple (same tenant_id-match form as every other
  table) without joining to `budgets`. The trade-off is that an
  alert event references the budget_id but the budget_id alone
  isn't enough to authorize the alert row — the RLS uses the
  denormalized tenant_id, which the evaluator must set correctly
  on every INSERT.

- `scope_value` is NULL for tenant-wide budgets (`scope_kind =
  'tenant'`). The unique constraint that enforces "one alert per
  threshold per period" intentionally does NOT include scope_value
  — uniqueness is on (budget_id, period_start, threshold_crossed),
  and the budget already encodes the scope. So NULL scope values
  don't break uniqueness (NULL != NULL is irrelevant here).

- `threshold_usd` is NUMERIC(10,2) because spend totals are
  computed in Decimal (`pricing.compute_cost_usd`) and storing
  ratios + spends as float would drift over a billing cycle.
  10 digits before / 2 after is ample headroom — Anthropic's
  largest enterprise customers spend ~7 figures monthly today.

- `threshold_crossed` is NUMERIC(3,2) — values are 0.70 / 0.85 /
  1.00. The 3,2 shape stores them as 0.70, 0.85, 1.00 exactly
  (vs. float drift to 0.7000000001).

- Soft-delete on `budgets` via `deleted_at`. Audit-chain principle:
  we never delete an alert that fired; cancelling a budget after
  the fact keeps its row + cancellation timestamp.

- `alert_recipients` is a `TEXT[]` array of email addresses.
  Cardinality is small (typically 1-5 recipients per budget) and
  the alert path doesn't need to JOIN by recipient. A normalized
  recipients table would add churn for no query benefit.

RLS
===

Same per-tenant pattern as workspaces / api_keys /
telemetry_records. Both tables get FORCE ROW LEVEL SECURITY +
USING + WITH CHECK on `tenant_id = current_setting('app.tenant_id')`.

`vargate_app` (the application role) gets DML automatically via the
ALTER DEFAULT PRIVILEGES from migration 0002. `vargate_scheduler`
(the cross-tenant beat role) is NOT extended for these tables —
the budget evaluator uses `scheduler_session_scope` to enumerate
tenants from the `tenants` table only, then `session_scope(tid)`
(vargate_app, RLS-bound) for each tenant's budgets + alert inserts.

Revision ID: 0019_create_budgets
Revises: 0018_create_api_keys
Create Date: 2026-05-14 19:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_create_budgets"
down_revision: Union[str, None] = "0018_create_api_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "budgets",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column("scope_value", sa.String(256), nullable=True),
        sa.Column("period", sa.String(16), nullable=False),
        sa.Column("threshold_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "alert_recipients",
            sa.dialects.postgresql.ARRAY(sa.String(320)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "scope_kind IN ('api_key', 'workspace', 'model', 'tenant')",
            name="ck_budgets_scope_kind",
        ),
        sa.CheckConstraint(
            "period IN ('monthly', 'weekly', 'daily')",
            name="ck_budgets_period",
        ),
        sa.CheckConstraint(
            "threshold_usd > 0",
            name="ck_budgets_threshold_positive",
        ),
        sa.CheckConstraint(
            # Tenant-wide budgets have NULL scope_value; every other
            # scope MUST have a concrete value (api_key_id /
            # workspace_id / model name).
            "(scope_kind = 'tenant' AND scope_value IS NULL) "
            "OR (scope_kind <> 'tenant' AND scope_value IS NOT NULL)",
            name="ck_budgets_scope_value_matches_kind",
        ),
    )
    # Partial index — most reads filter out soft-deleted rows, so
    # the index only carries live rows. Cardinality stays small.
    op.execute(
        "CREATE INDEX idx_budgets_tenant_live ON budgets (tenant_id) "
        "WHERE deleted_at IS NULL"
    )
    # RLS — same shape as workspaces / api_keys.
    op.execute("ALTER TABLE budgets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE budgets FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_budgets ON budgets
            USING (tenant_id::text = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
        """
    )

    op.create_table(
        "budget_alert_events",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "budget_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("budgets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Denormalized — RLS uses this, not a JOIN through budgets.
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column(
            "threshold_crossed",
            sa.Numeric(3, 2),
            nullable=False,
        ),
        sa.Column(
            "current_spend_usd",
            sa.Numeric(10, 2),
            nullable=False,
        ),
        sa.Column(
            "fired_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "acknowledged_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "acknowledged_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Dedup contract — the evaluator INSERTs with ON CONFLICT
        # DO NOTHING on this triple, so each threshold fires at most
        # once per (budget, period). Three thresholds (.70, .85,
        # 1.00) means up to three alerts per budget per period.
        sa.UniqueConstraint(
            "budget_id",
            "period_start",
            "threshold_crossed",
            name="uq_budget_alert_events_dedup",
        ),
        sa.CheckConstraint(
            "threshold_crossed IN (0.70, 0.85, 1.00)",
            name="ck_budget_alert_events_threshold_values",
        ),
    )
    # Partial index for the active-alerts table view (every UI load
    # filters for acknowledged_at IS NULL).
    op.execute(
        "CREATE INDEX idx_budget_alert_events_tenant_unack "
        "ON budget_alert_events (tenant_id, fired_at DESC) "
        "WHERE acknowledged_at IS NULL"
    )
    # Full-history index for paged "all alerts" view (ordered by
    # fired_at desc; tenant_id-first so RLS predicate prunes
    # efficiently).
    op.create_index(
        "idx_budget_alert_events_tenant_fired",
        "budget_alert_events",
        ["tenant_id", "fired_at"],
    )
    op.execute(
        "ALTER TABLE budget_alert_events ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE budget_alert_events FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        """
        CREATE POLICY tenant_isolation_budget_alert_events ON budget_alert_events
            USING (tenant_id::text = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_budget_alert_events "
        "ON budget_alert_events"
    )
    op.execute(
        "DROP INDEX IF EXISTS idx_budget_alert_events_tenant_unack"
    )
    op.drop_index(
        "idx_budget_alert_events_tenant_fired",
        table_name="budget_alert_events",
    )
    op.drop_table("budget_alert_events")

    op.execute("DROP POLICY IF EXISTS tenant_isolation_budgets ON budgets")
    op.execute("DROP INDEX IF EXISTS idx_budgets_tenant_live")
    op.drop_table("budgets")
