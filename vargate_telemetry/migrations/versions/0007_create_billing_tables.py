# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Create tenant_billing + billing_retry tables with RLS (T2.4).

Two tables co-located in one migration because they form a single
billing-wiring unit: `tenant_billing` maps a tenant to its Stripe
subscription item; `billing_retry` is the write-only failure queue
populated when a flush-time Stripe dispatch raises. Both follow the
standard ENABLE+FORCE RLS pattern with a tenant_isolation policy keyed
on `current_setting('app.tenant_id', true)`.

DML grants to `vargate_app` arrive automatically via the
`ALTER DEFAULT PRIVILEGES` clause set in `0002_create_app_role` — no
explicit GRANT needed here.

Revision ID: 0007_create_billing_tables
Revises: 0006_create_usage_records
Create Date: 2026-05-10 21:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007_create_billing_tables"
down_revision: Union[str, None] = "0006_create_usage_records"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenant_billing",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("subscription_item_id", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.execute("ALTER TABLE tenant_billing ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_billing FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_tenant_billing ON tenant_billing
          USING       (tenant_id = current_setting('app.tenant_id', true))
          WITH CHECK  (tenant_id = current_setting('app.tenant_id', true))
        """
    )

    op.create_table(
        "billing_retry",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("record_type", sa.String(32), nullable=False),
        sa.Column(
            "bucket_start",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("quantity", sa.BigInteger, nullable=False),
        sa.Column("last_error", sa.Text, nullable=False),
        sa.Column(
            "attempts",
            sa.Integer,
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_billing_retry_tenant",
        "billing_retry",
        ["tenant_id"],
    )
    op.create_index(
        "ix_billing_retry_tenant_created",
        "billing_retry",
        ["tenant_id", "created_at"],
    )

    op.execute("ALTER TABLE billing_retry ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE billing_retry FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_billing_retry ON billing_retry
          USING       (tenant_id = current_setting('app.tenant_id', true))
          WITH CHECK  (tenant_id = current_setting('app.tenant_id', true))
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_billing_retry ON billing_retry"
    )
    op.execute("ALTER TABLE billing_retry DISABLE ROW LEVEL SECURITY")
    op.drop_index(
        "ix_billing_retry_tenant_created", table_name="billing_retry"
    )
    op.drop_index("ix_billing_retry_tenant", table_name="billing_retry")
    op.drop_table("billing_retry")

    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_tenant_billing ON tenant_billing"
    )
    op.execute("ALTER TABLE tenant_billing DISABLE ROW LEVEL SECURITY")
    op.drop_table("tenant_billing")
