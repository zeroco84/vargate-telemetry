# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Create non-superuser app role for RLS-bound application traffic.

The first attempt at solving the RLS-bypass problem (an earlier draft of
this migration named `0002_drop_app_superuser`) tried to ALTER USER
CURRENT_USER NOSUPERUSER. Postgres rejects this with

    permission denied to alter role
    DETAIL:  The bootstrap user must have the SUPERUSER attribute.

— the bootstrap user, whoever POSTGRES_USER points to, is permanently
super. Removing that attribute could leave the cluster with no
superusers, and the kernel forbids it as a safety check regardless of
the current session's privileges.

The right pattern is to leave the bootstrap user alone and create a
separate **non-superuser** role for application traffic. `vargate_app`
is that role. It cannot connect directly (NOLOGIN) and never bypasses
RLS the way the bootstrap superuser would, but the bootstrap role is
granted membership in it so application code can SET ROLE vargate_app
for the lifetime of a transaction.

`vargate_telemetry.db.session_scope` issues `SET LOCAL ROLE vargate_app`
at the start of every transaction it manages, so application code
operates under the non-super role for the duration and reverts to the
bootstrap user afterward. Migrations themselves continue to run as the
bootstrap superuser, so DDL is unaffected.

`vargate_app` is granted full DML on every existing table plus a
`DEFAULT PRIVILEGES` clause so future tables created by the bootstrap
role pick up the same grants automatically. New tables therefore Just
Work — no need to remember to GRANT in every migration.

Revision ID: 0002_create_app_role
Revises: 0001_enable_rls
Create Date: 2026-05-09 02:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_create_app_role"
down_revision: Union[str, None] = "0001_enable_rls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent CREATE ROLE — useful when a partial state needs replay.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vargate_app') THEN
                CREATE ROLE vargate_app
                    NOLOGIN
                    NOSUPERUSER
                    NOINHERIT
                    NOCREATEDB
                    NOCREATEROLE;
            END IF;
        END $$;
        """
    )

    # Bootstrap role can SET ROLE vargate_app. Re-granting is a no-op.
    op.execute("GRANT vargate_app TO CURRENT_USER")

    # vargate_app needs USAGE on the schema and DML on every existing
    # table / sequence. Future tables / sequences inherit these grants
    # via ALTER DEFAULT PRIVILEGES below.
    op.execute("GRANT USAGE ON SCHEMA public TO vargate_app")
    op.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO vargate_app")
    op.execute("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO vargate_app")

    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT ALL ON TABLES TO vargate_app
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT ALL ON SEQUENCES TO vargate_app
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            REVOKE ALL ON TABLES FROM vargate_app
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            REVOKE ALL ON SEQUENCES FROM vargate_app
        """
    )
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM vargate_app")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM vargate_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM vargate_app")
    op.execute("REVOKE vargate_app FROM CURRENT_USER")
    op.execute("DROP ROLE IF EXISTS vargate_app")
