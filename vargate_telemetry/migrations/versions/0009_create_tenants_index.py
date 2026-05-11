# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Create tenants index + vargate_scheduler role (T3.4).

`tenants` is the small global index the Celery beat scheduler reads
to enumerate active tenants and fan out per-tenant pull tasks. It
has **no RLS** because the scheduler needs to see every tenant at
once — a fundamentally cross-tenant query that RLS would block.

To avoid the obvious risk (any RLS-bypassed table is a juicy target),
read access is restricted to a dedicated role:

- `vargate_scheduler` (NOLOGIN, NOSUPERUSER) — SELECT on `tenants`
  only, USAGE on the schema. No GRANT on any other table.
- `vargate_app` — explicitly REVOKE'd from `tenants` so the
  `ALTER DEFAULT PRIVILEGES` clause from 0002 doesn't auto-include it.

The bootstrap role retains full access for migrations and ops.

`vargate_telemetry.db.scheduler_session_scope` is the entry point that
issues `SET LOCAL ROLE vargate_scheduler`; the scheduler never opens
a session under `vargate_app`.

Revision ID: 0009_create_tenants_index
Revises: 0008_create_pull_state
Create Date: 2026-05-11 16:10:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_create_tenants_index"
down_revision: Union[str, None] = "0008_create_pull_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("region", sa.String(8), nullable=False),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "billing_status",
            sa.String(16),
            nullable=False,
            server_default="trial",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Idempotent CREATE ROLE — same pattern as 0002_create_app_role.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vargate_scheduler') THEN
                CREATE ROLE vargate_scheduler
                    NOLOGIN
                    NOSUPERUSER
                    NOINHERIT
                    NOCREATEDB
                    NOCREATEROLE;
            END IF;
        END $$;
        """
    )

    # Bootstrap role can SET ROLE vargate_scheduler.
    op.execute("GRANT vargate_scheduler TO CURRENT_USER")

    # Schema USAGE + SELECT on `tenants` only. No DML, no other tables.
    op.execute("GRANT USAGE ON SCHEMA public TO vargate_scheduler")
    op.execute("GRANT SELECT ON TABLE tenants TO vargate_scheduler")

    # `0002_create_app_role` set ALTER DEFAULT PRIVILEGES to grant ALL
    # on new tables to vargate_app. Undo that grant for `tenants` so
    # the app role cannot read it even if it tries.
    op.execute("REVOKE ALL ON TABLE tenants FROM vargate_app")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON TABLE tenants FROM vargate_scheduler")
    op.execute("REVOKE USAGE ON SCHEMA public FROM vargate_scheduler")
    op.execute("REVOKE vargate_scheduler FROM CURRENT_USER")
    op.execute("DROP ROLE IF EXISTS vargate_scheduler")
    op.drop_table("tenants")
