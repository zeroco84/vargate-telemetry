# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Add users.role for lightweight admin/member gating (TM4).

Adds a ``role`` column to ``users`` — ``'admin'`` or ``'member'``,
default ``'member'``. Admins may write budgets, map identities to
users, and change other users' roles; members are read + self-service
only.

Backfills the **earliest-created user per tenant** to ``'admin'`` so
existing single-operator tenants are never left without an admin (which
would lock them out of their own budget config). ``users`` has no RLS
(it's read by the unauth'd SSO callback path before any tenant context
exists — see ``models/users.py``), so this is a plain column add + data
backfill; no policy changes.

Going forward, ``POST /onboarding/select-region`` promotes the tenant's
provisioning user to ``'admin'`` at creation time.
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_add_user_role"
down_revision: Union[str, None] = "0021_create_interaction_topics"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "role",
            sa.String(length=16),
            nullable=False,
            server_default="member",
        ),
    )
    # Backfill: the earliest-created user in each tenant becomes admin so
    # no existing tenant ends up with zero admins. DISTINCT ON picks one
    # row per tenant; ties broken deterministically by id.
    op.execute(
        """
        UPDATE users SET role = 'admin'
        WHERE id IN (
            SELECT DISTINCT ON (tenant_id) id
            FROM users
            WHERE tenant_id IS NOT NULL
            ORDER BY tenant_id, created_at ASC, id ASC
        )
        """
    )


def downgrade() -> None:
    op.drop_column("users", "role")
