# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Create users + sessions tables for SSO sign-in (T4.2).

Both tables are **global** (not tenant-scoped). No RLS:

- `users` is read by the unauth'd SSO callback path that doesn't have
  a tenant context yet. Access control is via the JWT carried in the
  `ogma_session` cookie, decoded per request by the `current_user`
  FastAPI dependency.
- `sessions` is the refresh-token store (T4.x consumer; T4.2 just
  creates the schema). Same access shape as users.

Both tables get DML grants to `vargate_app` automatically via the
`ALTER DEFAULT PRIVILEGES` clause established in `0002_create_app_role`.

Revision ID: 0010_create_users_and_sessions
Revises: 0009_create_tenants_index
Create Date: 2026-05-11 17:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010_create_users_and_sessions"
down_revision: Union[str, None] = "0009_create_tenants_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("sso_provider", sa.String(32), nullable=False),
        sa.Column("sso_subject_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_login_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "sso_provider",
            "sso_subject_id",
            name="uq_users_provider_subject",
        ),
    )

    # Lookup index for "find user by email" diagnostics. Not unique:
    # the same email can have a google account AND a microsoft account,
    # which are two distinct rows.
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(128), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
