# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Create pull_state table with RLS (T3.4).

Per-tenant per-source-API cursor + last-status. Standard
tenant_isolation policy per docs/architecture/postgres-rls.md.
Composite primary key on (tenant_id, source_api) — the scheduler
looks up cursor by both before each pull cycle.

Revision ID: 0008_create_pull_state
Revises: 0007_create_billing_tables
Create Date: 2026-05-11 16:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_create_pull_state"
down_revision: Union[str, None] = "0007_create_billing_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pull_state",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("source_api", sa.String(32), primary_key=True),
        sa.Column("cursor", sa.String(512), nullable=True),
        sa.Column(
            "last_pulled_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_status", sa.String(32), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
    )

    op.execute("ALTER TABLE pull_state ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE pull_state FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_pull_state ON pull_state
          USING       (tenant_id = current_setting('app.tenant_id', true))
          WITH CHECK  (tenant_id = current_setting('app.tenant_id', true))
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_pull_state ON pull_state"
    )
    op.execute("ALTER TABLE pull_state DISABLE ROW LEVEL SECURITY")
    op.drop_table("pull_state")
