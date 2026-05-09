# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Create tenant_deks and encrypted_secrets with RLS (T1.7).

Both tables follow the convention from docs/architecture/postgres-rls.md:
ENABLE + FORCE + tenant-isolation policy with USING and WITH CHECK
clauses. The default privileges granted to vargate_app in
0002_create_app_role apply automatically to these new tables, so no
explicit GRANT is needed.

`tenant_deks` holds one row per tenant with the HSM-wrapped DEK plus
the label of the KEK that wrapped it. `encrypted_secrets` holds
(tenant_id, secret_name) -> (iv, ciphertext) tuples encrypted with the
tenant's DEK. The application-level seal/unseal API in
vargate_telemetry.crypto.seal is the only blessed writer.

Revision ID: 0003_create_encrypted_secrets
Revises: 0002_create_app_role
Create Date: 2026-05-09 03:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003_create_encrypted_secrets"
down_revision: Union[str, None] = "0002_create_app_role"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # tenant_deks — one row per tenant; tenant_id is the PK.
    op.create_table(
        "tenant_deks",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("wrapped_dek", sa.LargeBinary, nullable=False),
        sa.Column("kek_label", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.execute("ALTER TABLE tenant_deks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_deks FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_tenant_deks ON tenant_deks
          USING       (tenant_id = current_setting('app.tenant_id', true))
          WITH CHECK  (tenant_id = current_setting('app.tenant_id', true))
        """
    )

    # encrypted_secrets — per-tenant keyed secrets (admin keys, etc.)
    op.create_table(
        "encrypted_secrets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("secret_name", sa.String(128), nullable=False),
        sa.Column("iv", sa.LargeBinary, nullable=False),
        sa.Column("ciphertext", sa.LargeBinary, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_rotated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "secret_name",
            name="uq_encrypted_secrets_tenant_name",
        ),
    )
    op.create_index(
        "ix_encrypted_secrets_tenant",
        "encrypted_secrets",
        ["tenant_id"],
    )
    op.execute("ALTER TABLE encrypted_secrets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE encrypted_secrets FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_encrypted_secrets ON encrypted_secrets
          USING       (tenant_id = current_setting('app.tenant_id', true))
          WITH CHECK  (tenant_id = current_setting('app.tenant_id', true))
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_encrypted_secrets "
        "ON encrypted_secrets"
    )
    op.execute("ALTER TABLE encrypted_secrets DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_encrypted_secrets_tenant", table_name="encrypted_secrets")
    op.drop_table("encrypted_secrets")

    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_tenant_deks ON tenant_deks"
    )
    op.execute("ALTER TABLE tenant_deks DISABLE ROW LEVEL SECURITY")
    op.drop_table("tenant_deks")
