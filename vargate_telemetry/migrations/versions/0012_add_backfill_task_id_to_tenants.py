# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Record the initial backfill's Celery task id on the tenants row (T4.6).

`POST /onboarding/start-backfill` enqueues `backfill_admin_for_tenant`
and stores the returned task id here. The frontend polls
`/onboarding/backfill-status/{task_id}` to render progress; the
stored id is what makes that endpoint's tenant-scoping possible —
we accept a task_id only if it matches the current user's tenant.

NULLABLE: until step 5 of onboarding completes, no backfill has been
scheduled and the column is NULL. After the first successful
start-backfill call it carries the task id forever (re-running the
backfill manually after T4.6 would overwrite, but T4.6 ships with
idempotency on the existing id — see the route impl).

Revision ID: 0012_backfill_task_id
Revises: 0011_add_tenant_id_to_users
Create Date: 2026-05-11 19:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
# (Capped at 32 chars by the default alembic_version column width.)
revision: str = "0012_backfill_task_id"
down_revision: Union[str, None] = "0011_add_tenant_id_to_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("initial_backfill_task_id", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "initial_backfill_task_id")
