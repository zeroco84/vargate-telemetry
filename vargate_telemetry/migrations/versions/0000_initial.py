# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""initial empty migration

Establishes the alembic version table; no schema changes. T1.5 enables
RLS, T1.7 adds encrypted_secrets and tenant_deks, and so on.

Revision ID: 0000_initial
Revises:
Create Date: 2026-05-09 00:00:00.000000

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "0000_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
