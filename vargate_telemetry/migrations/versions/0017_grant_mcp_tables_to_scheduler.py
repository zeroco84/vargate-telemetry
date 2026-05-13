# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Grant ``vargate_scheduler`` DML on the MCP OAuth tables (TM1 hotfix).

0016 created ``mcp_oauth_clients`` and ``mcp_access_tokens`` and
``ALTER DEFAULT PRIVILEGES`` from 0002 picked them up for
``vargate_app`` automatically. But the MCP server's hot path uses
``scheduler_session_scope`` (the cross-tenant, no-RLS role) for
two reasons:

  1. DCR (``mcp_oauth_clients``) is a global registry — there's no
     tenant_id to scope by at registration time.
  2. The token verifier looks up a row by ``token_hash`` BEFORE the
     identity is known, so ``app.tenant_id`` can't be set yet.

``vargate_scheduler`` was narrowed in 0009 to ``SELECT ON tenants``
only. This migration extends it with SELECT + INSERT + UPDATE +
DELETE on the two MCP tables — the minimum surface the OAuth
routes + verifier need.

The grant is intentionally scoped to MCP tables only. We do NOT
broaden ``vargate_scheduler`` with a blanket ``ALL TABLES IN SCHEMA``
because that would defeat the role's "read-only on tenants" posture
for the actual scheduler code.

Revision ID: 0017_grant_mcp_tables_to_scheduler
Revises: 0016_mcp_oauth_tables
Create Date: 2026-05-13 16:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0017_grant_mcp_tables_to_scheduler"
down_revision: Union[str, None] = "0016_mcp_oauth_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # MCP OAuth surface needs:
    #   - INSERT into mcp_oauth_clients on DCR (/register)
    #   - SELECT from mcp_oauth_clients on /authorize (client lookup)
    #   - INSERT into mcp_access_tokens on /token (mint)
    #   - SELECT from mcp_access_tokens on every tool call (verifier)
    #   - UPDATE on revocation (future kill-switch)
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE mcp_oauth_clients "
        "TO vargate_scheduler"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE mcp_access_tokens "
        "TO vargate_scheduler"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE mcp_access_tokens "
        "FROM vargate_scheduler"
    )
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE mcp_oauth_clients "
        "FROM vargate_scheduler"
    )
