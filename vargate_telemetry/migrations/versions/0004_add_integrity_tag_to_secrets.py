# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Add integrity_tag columns to tenant_deks and encrypted_secrets (T2.0).

Closes the AEAD gap left by T1.6's CBC-PAD pivot. T2.0's HMAC over
`tenant_id || ":" || wrapped` lives in this column on both tables;
seal/unseal verify it before any decryption attempt.

Pre-existing rows (in dev or test databases that have run any T1.7
seal/unseal flow) get a server_default of 32 zero bytes for the
duration of the schema change. The default is dropped immediately
afterward, so future inserts must supply a real tag. Any pre-existing
row will fail integrity verification on the next unseal — which is
correct fail-closed behaviour, not a bug. Re-seal those rows to
backfill a real tag.

Revision ID: 0004_add_integrity_tag_to_secrets
Revises: 0003_create_encrypted_secrets
Create Date: 2026-05-09 04:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_add_integrity_tag_to_secrets"
down_revision: Union[str, None] = "0003_create_encrypted_secrets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 32 zero-bytes as a bytea literal — the transitional default for any
# rows that exist before the column does. Real tags overwrite this on
# the next seal_secret call.
_ZERO_TAG_SQL = sa.text("decode(repeat('00', 32), 'hex')")


def upgrade() -> None:
    op.add_column(
        "tenant_deks",
        sa.Column(
            "integrity_tag",
            sa.LargeBinary,
            nullable=False,
            server_default=_ZERO_TAG_SQL,
        ),
    )
    op.alter_column("tenant_deks", "integrity_tag", server_default=None)

    op.add_column(
        "encrypted_secrets",
        sa.Column(
            "integrity_tag",
            sa.LargeBinary,
            nullable=False,
            server_default=_ZERO_TAG_SQL,
        ),
    )
    op.alter_column("encrypted_secrets", "integrity_tag", server_default=None)


def downgrade() -> None:
    op.drop_column("encrypted_secrets", "integrity_tag")
    op.drop_column("tenant_deks", "integrity_tag")
