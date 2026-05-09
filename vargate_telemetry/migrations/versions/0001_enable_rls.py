# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Enable Postgres RLS as defense-in-depth on tenant-owned tables (T1.5).

Creates a placeholder `_rls_canary` table that exercises the standard
RLS pattern. Every tenant-owned Telemetry table created in later
migrations follows the same pattern; see docs/architecture/postgres-rls.md
for the convention.

Three things make this pattern correct:

  1. ENABLE ROW LEVEL SECURITY turns RLS on for the table.
  2. FORCE ROW LEVEL SECURITY makes RLS apply to the *table owner* too.
     Without FORCE, the connecting user (which is the owner of every
     Telemetry table) bypasses RLS — exactly the wrong default for
     defense-in-depth.
  3. The policy uses `current_setting('app.tenant_id', true)`. The `true`
     second argument tells Postgres to return NULL when the GUC is unset
     instead of erroring; `tenant_id = NULL` is NULL (not true), so an
     unset GUC yields zero rows. This is the desired fail-closed default.

Both USING (read) and WITH CHECK (write) clauses are set so an INSERT
under tenant-A cannot plant a row tagged for tenant-B.

Revision ID: 0001_enable_rls
Revises: 0000_initial
Create Date: 2026-05-09 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_enable_rls"
down_revision: Union[str, None] = "0000_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE _rls_canary (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX _rls_canary_tenant_idx ON _rls_canary (tenant_id)")

    op.execute("ALTER TABLE _rls_canary ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE _rls_canary FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY tenant_isolation_canary ON _rls_canary
          USING (tenant_id = current_setting('app.tenant_id', true))
          WITH CHECK (tenant_id = current_setting('app.tenant_id', true))
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_canary ON _rls_canary")
    op.execute("ALTER TABLE _rls_canary DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE IF EXISTS _rls_canary")
