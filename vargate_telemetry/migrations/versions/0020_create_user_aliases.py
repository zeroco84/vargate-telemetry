# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Add `user_aliases` table (TM3 Phase C1).

The unique-to-Ogma analytic: a single human shows up under THREE
disconnected identities in Anthropic's view —

  - Admin API:      key-level only (no per-user attribution)
  - Code Analytics: actor email (or api_key_name for service keys)
  - MCP (chat):      the Ogma SSO user's email

Ogma stitches them. ``user_aliases`` maps
``(tenant_id, source_api, source_identifier)`` → ``users.id`` so
the ``/users`` view can roll a person's activity up across every
surface they touch.

Auto-match (TM3 conventions, see telemetry CLAUDE.md)
====================================================

When the reconciler sees a ``source_identifier`` not yet aliased,
it attempts email-equality against ``users.email`` (tenant-scoped —
``users`` has no RLS, so the match MUST filter by tenant_id or it
would link across tenants). A single match → link automatically
with ``auto_matched = true``. No match (or a non-email identifier
like an api_key_name) → the row lands with ``user_id = NULL`` and
surfaces in the admin's "Unmapped activity" panel.

Manual linking sets ``auto_matched = false`` — which protects the
row from a later auto-match attempt that might disagree. The
reconciler's UPSERT only re-touches rows that are still
``auto_matched = true AND user_id IS NULL`` (newly-matchable
unmapped rows).

Never-delete
============

Deleting a user nulls ``user_id`` on their alias rows (ON DELETE
SET NULL) but keeps the rows so historical telemetry stays grouped
under the alias identifier. Same audit-ledger posture as budgets'
created_by_user_id.

Revision ID: 0020_create_user_aliases
Revises: 0019_create_budgets
Create Date: 2026-05-14 20:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_create_user_aliases"
down_revision: Union[str, None] = "0019_create_budgets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_aliases",
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
        # Nullable: an unmapped alias (no matching Ogma user yet).
        # ON DELETE SET NULL keeps the alias row when the user is
        # deleted so historical telemetry stays grouped.
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_api", sa.String(32), nullable=False),
        # The actor's natural identifier — email for user actors,
        # api_key_name / api_key_id for service actors. Up to 320
        # chars to hold a full email (RFC 5321 max local+domain).
        sa.Column("source_identifier", sa.String(320), nullable=False),
        sa.Column(
            "auto_matched",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
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
        sa.UniqueConstraint(
            "tenant_id",
            "source_api",
            "source_identifier",
            name="uq_user_aliases_identity",
        ),
    )
    # Index for the "all aliases for this user" rollup (the /users
    # detail view groups telemetry by user_id via these rows).
    op.create_index(
        "idx_user_aliases_tenant_user",
        "user_aliases",
        ["tenant_id", "user_id"],
    )
    # Partial index for the "Unmapped activity" panel — the index
    # only carries unmapped rows, which is what that panel queries.
    op.execute(
        "CREATE INDEX idx_user_aliases_unmapped "
        "ON user_aliases (tenant_id) WHERE user_id IS NULL"
    )
    # RLS — same per-tenant pattern as every other tenant table.
    op.execute("ALTER TABLE user_aliases ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_aliases FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_user_aliases ON user_aliases
            USING (tenant_id::text = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_user_aliases ON user_aliases"
    )
    op.execute("DROP INDEX IF EXISTS idx_user_aliases_unmapped")
    op.drop_index(
        "idx_user_aliases_tenant_user", table_name="user_aliases"
    )
    op.drop_table("user_aliases")
