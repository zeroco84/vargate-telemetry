# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Create telemetry_records table with RLS (T2.1).

The shape of one ingested Anthropic-side event. Chain columns
(chain_seq, chain_prev_hash, chain_self_hash) are populated by T2.2's
chain producer that delegates to the vargate-audit-chain package.

Constraints worth noting:
  - (tenant_id, source_api, external_id) UNIQUE — dedup. Re-pulling the
    same record from Anthropic is a no-op.
  - (tenant_id, chain_seq) UNIQUE — chain ordering invariant. Two
    records cannot occupy the same chain position within one tenant.
  - Standard tenant_isolation policy + ENABLE + FORCE per
    docs/architecture/postgres-rls.md.

Revision ID: 0005_create_telemetry_records
Revises: 0004_add_integrity_tag
Create Date: 2026-05-09 05:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005_create_telemetry_records"
down_revision: Union[str, None] = "0004_add_integrity_tag"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telemetry_records",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("record_type", sa.String(32), nullable=False),
        sa.Column("source_api", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(256), nullable=False),
        sa.Column("subject_user_id", sa.String(128), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("content_ref", sa.String(512), nullable=True),
        sa.Column("content_hash", sa.LargeBinary, nullable=False),
        sa.Column("metadata", postgresql.JSONB, nullable=False),
        sa.Column("chain_seq", sa.BigInteger, nullable=False),
        sa.Column("chain_prev_hash", sa.LargeBinary, nullable=False),
        sa.Column("chain_self_hash", sa.LargeBinary, nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "source_api",
            "external_id",
            name="uq_telemetry_records_dedup",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "chain_seq",
            name="uq_telemetry_records_chain",
        ),
    )

    op.create_index(
        "ix_telemetry_records_tenant",
        "telemetry_records",
        ["tenant_id"],
    )
    op.create_index(
        "ix_telemetry_records_tenant_occurred",
        "telemetry_records",
        ["tenant_id", "occurred_at"],
    )

    op.execute("ALTER TABLE telemetry_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE telemetry_records FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_telemetry_records ON telemetry_records
          USING       (tenant_id = current_setting('app.tenant_id', true))
          WITH CHECK  (tenant_id = current_setting('app.tenant_id', true))
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_telemetry_records "
        "ON telemetry_records"
    )
    op.execute("ALTER TABLE telemetry_records DISABLE ROW LEVEL SECURITY")
    op.drop_index(
        "ix_telemetry_records_tenant_occurred",
        table_name="telemetry_records",
    )
    op.drop_index(
        "ix_telemetry_records_tenant",
        table_name="telemetry_records",
    )
    op.drop_table("telemetry_records")
