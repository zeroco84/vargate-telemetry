# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Create usage_records table with RLS (T2.3).

Durable counter table backing billing. Standard tenant_isolation
policy per docs/architecture/postgres-rls.md. UNIQUE on (tenant_id,
bucket_start, record_type) is the upsert key — the flush task uses
ON CONFLICT DO UPDATE so re-flushing the same bucket folds into the
existing row without double-counting.

Revision ID: 0006_create_usage_records
Revises: 0005_create_telemetry_records
Create Date: 2026-05-10 19:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006_create_usage_records"
down_revision: Union[str, None] = "0005_create_telemetry_records"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "usage_records",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "bucket_start",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("record_type", sa.String(32), nullable=False),
        sa.Column("record_count", sa.BigInteger, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "bucket_start",
            "record_type",
            name="uq_usage_records_tenant_bucket_type",
        ),
    )

    op.create_index(
        "ix_usage_records_tenant",
        "usage_records",
        ["tenant_id"],
    )
    op.create_index(
        "ix_usage_records_tenant_bucket",
        "usage_records",
        ["tenant_id", "bucket_start"],
    )

    op.execute("ALTER TABLE usage_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE usage_records FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_usage_records ON usage_records
          USING       (tenant_id = current_setting('app.tenant_id', true))
          WITH CHECK  (tenant_id = current_setting('app.tenant_id', true))
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_usage_records ON usage_records"
    )
    op.execute("ALTER TABLE usage_records DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_usage_records_tenant_bucket", table_name="usage_records")
    op.drop_index("ix_usage_records_tenant", table_name="usage_records")
    op.drop_table("usage_records")
