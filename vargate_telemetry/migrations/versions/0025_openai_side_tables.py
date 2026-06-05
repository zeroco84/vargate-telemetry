# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Add OpenAI side tables — `openai_projects`, `openai_api_keys`,
`openai_users` (TM8 Phase B).

OpenAI is Ogma's first non-Anthropic vendor. These three tables are
the OpenAI analogue of the Anthropic ``workspaces`` (migration 0015)
and ``api_keys`` (migration 0018) side tables: opaque vendor IDs flow
into ``telemetry_records`` from the usage/costs pulls, and these
tables resolve those IDs to human-friendly names (and, for users, to
an email for cross-vendor alias matching).

Population
==========

The OpenAI projects pull task (``tasks/pull_openai_projects.py``)
calls the Admin API list endpoints once per tenant and upserts every
row via raw ``INSERT ... ON CONFLICT ... DO UPDATE`` — exactly the
``_sync_workspaces`` / ``_sync_api_keys`` posture in
``tasks/pull_admin.py``. Like those, there is **no ORM model**: the
side tables are raw-SQL-only (``vargate_telemetry/models/`` ships no
``Workspace`` / ``ApiKey`` class either). Idempotent; never deletes —
rows for projects/keys/users that disappear from the org keep their
last-known values so historical telemetry stays resolvable.

Recon mapping (see ``docs/sprints/TM8-openai-recon.md`` §4)
===========================================================

  - ``openai_projects`` ← ``GET /v1/organization/projects``
        id → project_id, name, status, created_at → created_at_openai
  - ``openai_api_keys`` ← ``GET /v1/organization/projects/{id}/api_keys``
        id → api_key_id, project_id, name, created_at →
        created_at_openai, last_used_at (owner is informational only)
  - ``openai_users`` ← ``GET /v1/organization/users``
        id → openai_user_id, email (PII), name, role,
        added_at → (synced_at is ours, not the vendor's added_at)

The ``email`` column is what ``user_aliases`` matches on for the
cross-vendor user rollup; OpenAI source records carry the email into
the alias ``source_identifier``.

RLS
===

RLS-scoped per tenant, same pattern as every other tenant-owned
table (``ENABLE`` + ``FORCE ROW LEVEL SECURITY`` + a
``tenant_isolation_<table>`` policy keyed on
``current_setting('app.tenant_id', true)``). Mirrors migration 0020.

``source_api`` needs no DDL — it is a ``TEXT`` column, not a Postgres
enum (migration 0016 chose this deliberately). The new source values
``openai_admin_usage`` / ``openai_admin_costs`` / ``openai_audit_logs``
are inserted as plain strings into ``telemetry_records.source_api``.

Revision ID: 0025_openai_side_tables
Revises: 0024_budget_alert_kind
Create Date: 2026-06-05 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025_openai_side_tables"
down_revision: Union[str, None] = "0024_budget_alert_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── openai_projects ──────────────────────────────────────────────
    op.create_table(
        "openai_projects",
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # OpenAI-issued project id, e.g. ``proj_XXXX``; opaque to us.
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        # ``active`` / ``archived`` etc. — surfaced so the UI can dim
        # archived projects. Nullable to match the vendor's optional
        # field semantics.
        sa.Column("status", sa.String(32), nullable=True),
        # The project's vendor-side creation time (from ``created_at``,
        # a unix epoch the pull task converts to a tz-aware datetime).
        sa.Column(
            "created_at_openai",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # When Ogma last upserted this row (ours, not the vendor's).
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "project_id"),
    )
    op.create_index(
        "ix_openai_projects_tenant",
        "openai_projects",
        ["tenant_id"],
    )

    # ── openai_api_keys ──────────────────────────────────────────────
    op.create_table(
        "openai_api_keys",
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # OpenAI-issued key id, e.g. ``key_XXXX``; opaque to us.
        sa.Column("api_key_id", sa.String(128), nullable=False),
        # The project this key belongs to; nullable to tolerate keys
        # the org enumeration can't attribute to a project.
        sa.Column("project_id", sa.String(128), nullable=True),
        sa.Column("name", sa.String(256), nullable=True),
        sa.Column(
            "created_at_openai",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "api_key_id"),
    )
    op.create_index(
        "ix_openai_api_keys_tenant",
        "openai_api_keys",
        ["tenant_id"],
    )

    # ── openai_users ─────────────────────────────────────────────────
    op.create_table(
        "openai_users",
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # OpenAI-issued user id, e.g. ``user-XXXX``; opaque to us.
        sa.Column("openai_user_id", sa.String(128), nullable=False),
        # PII. Up to 320 chars to hold a full email (RFC 5321 max
        # local+domain); this is the cross-vendor alias match key.
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("name", sa.String(256), nullable=True),
        # ``owner`` / ``reader`` etc.
        sa.Column("role", sa.String(32), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "openai_user_id"),
    )
    op.create_index(
        "ix_openai_users_tenant",
        "openai_users",
        ["tenant_id"],
    )

    # ── RLS — one policy per table, same pattern as migration 0020 ───
    for table in ("openai_projects", "openai_api_keys", "openai_users"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation_{table} ON {table}
                USING (tenant_id::text = current_setting('app.tenant_id', true))
                WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
            """
        )


def downgrade() -> None:
    # Drop policies first, then indexes + tables (reverse of upgrade).
    for table in ("openai_users", "openai_api_keys", "openai_projects"):
        op.execute(
            f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}"
        )

    op.drop_index("ix_openai_users_tenant", table_name="openai_users")
    op.drop_table("openai_users")

    op.drop_index(
        "ix_openai_api_keys_tenant", table_name="openai_api_keys"
    )
    op.drop_table("openai_api_keys")

    op.drop_index(
        "ix_openai_projects_tenant", table_name="openai_projects"
    )
    op.drop_table("openai_projects")
