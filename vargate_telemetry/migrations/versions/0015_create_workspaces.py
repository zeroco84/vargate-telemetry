# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Add `workspaces` table (T5.5.6).

The Anthropic Admin API's usage endpoint emits ``workspace_id`` as a
vendor opaque identifier (e.g., ``wrkspc_01HABC...``). Customers
identify their workspaces by human-friendly **names** (set in
claude.ai). T5.5.6 introduces this table so the Usage view can
render names instead of IDs.

Population
==========

The backfill task calls ``client.list_workspaces()`` once per tenant
and upserts every row. The pull task does the same on every
incremental run; an O(workspaces-per-org) hit is negligible.

RLS-scoped per tenant. Same pattern as ``encrypted_secrets`` and
``telemetry_records``: read/write requires ``app.tenant_id`` to
match the row.

Revision ID: 0015_create_workspaces
Revises: 0014_content_size_bytes
Create Date: 2026-05-13 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_create_workspaces"
down_revision: Union[str, None] = "0014_content_size_bytes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
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
        sa.PrimaryKeyConstraint("tenant_id", "workspace_id"),
    )
    op.create_index(
        "ix_workspaces_tenant",
        "workspaces",
        ["tenant_id"],
    )
    # RLS — same pattern as telemetry_records / encrypted_secrets.
    op.execute("ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workspaces FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_workspaces ON workspaces
            USING (tenant_id::text = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_workspaces ON workspaces"
    )
    op.drop_index("ix_workspaces_tenant", table_name="workspaces")
    op.drop_table("workspaces")
