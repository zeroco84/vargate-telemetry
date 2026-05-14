# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""TM1 — MCP connector OAuth state tables.

Two new tables, both per-tenant RLS-protected so the MCP server's
token validator can lookup tokens for one tenant without leaking
across:

- ``mcp_oauth_clients`` — Dynamic Client Registration (RFC 7591)
  records. Claude self-registers on first connect; the row carries
  hashed client secret + the redirect URIs that Anthropic permits.
- ``mcp_access_tokens`` — issued bearer tokens. Hashed (SHA-256)
  on insert so a DB read doesn't leak live credentials. Audience-
  bound (RFC 8707 ``resource``) to ``mcp.ogma.vargate.ai``.

NOTE on source_api: TM1's plan called for adding 'mcp' to a
``source_api_enum`` PG enum. The actual schema (T2.x) keeps
``source_api`` as ``character varying(64)`` — no enum. So this
migration only adds the two OAuth tables; 'mcp' becomes a usable
``source_api`` value without any DDL.

Per the project rule ``never delete files``: the downgrade soft-
deletes (drops indices, leaves tables intact for forensic
recovery if needed). Hard-drop tables only when manually
greenlit.

Revision ID: 0016_mcp_oauth_tables
Revises: 0015_create_workspaces
Create Date: 2026-05-13 14:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0016_mcp_oauth_tables"
down_revision: Union[str, None] = "0015_create_workspaces"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── mcp_oauth_clients ──────────────────────────────────────────
    # Per-row RLS NOT applied: client registrations are global, not
    # tenant-scoped. A Claude client_id is the same record no matter
    # which tenant's user later authorizes against it. (Tenant binding
    # happens on the TOKEN row, not the client row.)
    op.create_table(
        "mcp_oauth_clients",
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("client_secret_hash", sa.String(128), nullable=False),
        sa.Column("client_name", sa.String(256), nullable=False),
        sa.Column(
            "redirect_uris",
            sa.ARRAY(sa.String(512)),
            nullable=False,
        ),
        sa.Column(
            "grant_types",
            sa.ARRAY(sa.String(64)),
            nullable=False,
        ),
        sa.Column(
            "response_types",
            sa.ARRAY(sa.String(64)),
            nullable=False,
        ),
        sa.Column(
            "token_endpoint_auth_method",
            sa.String(64),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Soft-delete only — see module docstring.
        sa.Column(
            "deleted_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.PrimaryKeyConstraint("client_id"),
    )

    # ── mcp_access_tokens ──────────────────────────────────────────
    # Tenant-bound, RLS-protected. Token validator runs inside a
    # session_scope(tenant_id) opened from the token's tenant claim
    # — but bootstrapping that lookup needs a way to find the row
    # WITHOUT app.tenant_id set. We solve that via two indexed
    # columns + a token_hash PK: validator uses
    # scheduler_session_scope (no RLS gate), reads the row by hash,
    # then opens a session_scope(token.tenant_id) for the actual
    # tool-handler write path.
    op.create_table(
        "mcp_access_tokens",
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column(
            "client_id",
            sa.String(64),
            sa.ForeignKey(
                "mcp_oauth_clients.client_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("user_email", sa.String(256), nullable=False),
        # RFC 8707 audience binding.
        sa.Column("resource", sa.String(512), nullable=False),
        sa.Column(
            "scopes",
            sa.ARRAY(sa.String(64)),
            nullable=True,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "refresh_token_hash",
            sa.String(64),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "revoked_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.PrimaryKeyConstraint("token_hash"),
    )

    # Hot-path index for the token validator. Expires + revoked
    # filtering combined with the PK lookup means a token-presented
    # request is a single index hit on the WHERE clause:
    # ``WHERE token_hash = $1 AND revoked_at IS NULL AND expires_at > now()``.
    op.create_index(
        "ix_mcp_tokens_expires",
        "mcp_access_tokens",
        ["expires_at"],
    )
    op.create_index(
        "ix_mcp_tokens_tenant",
        "mcp_access_tokens",
        ["tenant_id"],
    )
    op.create_index(
        "ix_mcp_tokens_refresh",
        "mcp_access_tokens",
        ["refresh_token_hash"],
    )


def downgrade() -> None:
    # Soft-downgrade: drop indices only. Tables remain so a forensic
    # query can still reach historical token + client registrations
    # if we ever need to investigate a revoked-token incident after
    # backing out the migration.
    op.drop_index("ix_mcp_tokens_refresh", table_name="mcp_access_tokens")
    op.drop_index("ix_mcp_tokens_tenant", table_name="mcp_access_tokens")
    op.drop_index("ix_mcp_tokens_expires", table_name="mcp_access_tokens")
