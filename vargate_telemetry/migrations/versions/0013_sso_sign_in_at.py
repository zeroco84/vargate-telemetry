# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Add `users.sso_sign_in_at` for time-to-first-pull metric (T4.7).

The Prom histogram ``vargate_onboarding_time_to_first_pull_seconds`` is
``now() - users.sso_sign_in_at`` computed when the first row lands in
``telemetry_records`` for the tenant. The column is set on every SSO
callback (so the most recent sign-in wins — matches the user's lived
"started the flow now" intent if they bounced and re-entered).

NULLABLE because:
  - Pre-existing rows (none in prod yet, but the migration is
    forward-only) won't have a value.
  - First-pull observation skips when the column is NULL — no
    half-real "time since epoch" garbage.

Revision id capped at 32 chars per the alembic_version column width.

Revision ID: 0013_sso_sign_in_at
Revises: 0012_backfill_task_id
Create Date: 2026-05-11 21:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_sso_sign_in_at"
down_revision: Union[str, None] = "0012_backfill_task_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "sso_sign_in_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "sso_sign_in_at")
