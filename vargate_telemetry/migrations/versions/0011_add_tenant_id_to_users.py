# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Add `tenant_id` to `users` so SSO sessions bind to a tenant (T4.5).

The column is NULLABLE because a user exists during the onboarding
window before they've chosen a region. Once `POST /onboarding/select-
region` succeeds, the column is set to the freshly provisioned
`tenant_id` and the session JWT is reissued carrying the same value
in its `tenant_id` claim.

No FK to `tenants`: the `tenants` table is the scheduler-readable
index with a role split (vargate_app has been REVOKED from it in
0009). A foreign key would require granting REFERENCES privileges
back to vargate_app, which would defeat the split. The onboarding
endpoint is the only writer of `users.tenant_id`, and it always
INSERTs the matching `tenants` row in the same transaction — so the
invariant is enforced at the application layer instead.

A non-unique index supports the "fetch user by tenant" lookup the
scheduler-free fast path uses; many users can share one tenant in
the future (multi-seat orgs), so the index is non-unique.

Revision ID: 0011_add_tenant_id_to_users
Revises: 0010_create_users_and_sessions
Create Date: 2026-05-11 18:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_add_tenant_id_to_users"
down_revision: Union[str, None] = "0010_create_users_and_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("tenant_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_column("users", "tenant_id")
