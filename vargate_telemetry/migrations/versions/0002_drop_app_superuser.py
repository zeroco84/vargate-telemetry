# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Drop SUPERUSER from the application role so RLS actually applies.

The official `postgres` Docker image creates POSTGRES_USER as a superuser
by default. SUPERUSERS BYPASS RLS regardless of ENABLE / FORCE settings,
which silently neuters the tenant-isolation policies installed in
0001_enable_rls. The bug surfaces immediately if you actually exercise
the policy (test_rls_blocks_unset_tenant returns the row count instead
of zero).

This migration strips SUPERUSER from the connecting role. Afterwards the
role retains DDL/DML privileges via table ownership and is subject to
RLS like any other user. Subsequent migrations all run as a
non-superuser owner, which is the desired posture for any role that
backs application traffic.

Reversal requires another superuser on the cluster, so we leave the
downgrade as a no-op and let an operator hand-roll it if ever needed.

Revision ID: 0002_drop_app_superuser
Revises: 0001_enable_rls
Create Date: 2026-05-09 01:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_drop_app_superuser"
down_revision: Union[str, None] = "0001_enable_rls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `CURRENT_USER` resolves to whatever role the migration is running
    # as, so this works regardless of POSTGRES_USER's exact value.
    op.execute("ALTER USER CURRENT_USER NOSUPERUSER")


def downgrade() -> None:
    # Cannot self-restore SUPERUSER from a non-superuser session; leave
    # downgrade as a deliberate no-op.
    pass
