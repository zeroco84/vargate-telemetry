# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Add `telemetry_records.content_size_bytes` (T5.1).

The other two T5.1-related columns (`content_ref`, `content_hash`)
already exist — they shipped in T2.1's migration 0005, which
anticipated the content layer. This migration only adds the third:
``content_size_bytes`` — the *uncompressed plaintext* size in bytes,
useful for ops capacity planning and for the dashboard's "this
record holds X KB of content" display. NULL for content-less
records (the Admin API usage buckets), populated by
``vargate_telemetry.storage.content.store_content`` when content
lands.

NOT in the chain canonical bytes
================================

Deliberately. Tamper-detection on the content payload is already
covered by ``content_hash`` (SHA-256 of plaintext), which is in the
chain canonical bytes from T2.1 onward. ``content_size_bytes`` is an
operational metric, not an integrity field — including it would
expand the chain hash surface without strengthening any actual
detection property (a SHA-256 collision is infeasible regardless of
size).

Revision id capped at 32 chars per the alembic_version column width.

Revision ID: 0014_content_size_bytes
Revises: 0013_sso_sign_in_at
Create Date: 2026-05-12 14:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_content_size_bytes"
down_revision: Union[str, None] = "0013_sso_sign_in_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "telemetry_records",
        sa.Column(
            "content_size_bytes",
            sa.BigInteger(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("telemetry_records", "content_size_bytes")
