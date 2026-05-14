# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Add `api_keys` table (TM3 Phase A4).

The Anthropic Admin API's usage report emits `api_key_id` (an opaque
vendor ID, e.g. ``apikey_01HABC...``) but does NOT include the API
key NAME — that has to come from a separate fetch to
``GET /v1/organizations/api_keys``. This table mirrors the
`workspaces` pattern from migration 0015 — synced at backfill +
pull time, joined into the API Usage SQL to render names.

Population
==========

`_sync_api_keys` in `tasks/pull_admin.py` upserts every API key
returned by the Admin API's list endpoint. Same idempotency
posture as workspaces: ON CONFLICT updates the name + status +
updated_at; never deletes (keys that disappear from the org keep
their last-known row for forensic historical resolution).

RLS-scoped per tenant. Same pattern as `workspaces` and
`telemetry_records`: read/write requires ``app.tenant_id`` to
match the row.

Revision ID: 0018_create_api_keys
Revises: 0017_grant_mcp_scheduler
Create Date: 2026-05-14 18:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_create_api_keys"
down_revision: Union[str, None] = "0017_grant_mcp_scheduler"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        # Anthropic-issued ID; opaque to us. Up to 128 chars to leave
        # headroom — observed values are ~50 chars.
        sa.Column("api_key_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        # Status surfaced in the UI so the frontend can dim
        # archived/expired keys differently from active ones.
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="active",
        ),
        # Workspace_id is null for keys belonging to the default
        # workspace; matches the Admin API's `null` semantics.
        sa.Column(
            "workspace_id", sa.String(64), nullable=True
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
        sa.PrimaryKeyConstraint("tenant_id", "api_key_id"),
    )
    op.create_index(
        "ix_api_keys_tenant",
        "api_keys",
        ["tenant_id"],
    )
    # RLS — same pattern as telemetry_records / workspaces.
    op.execute("ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE api_keys FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_api_keys ON api_keys
            USING (tenant_id::text = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_api_keys ON api_keys"
    )
    op.drop_index("ix_api_keys_tenant", table_name="api_keys")
    op.drop_table("api_keys")
