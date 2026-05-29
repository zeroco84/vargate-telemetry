# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Add `interaction_topics` table (TM4 Track D — activity categorization).

Topic classifications for MCP interaction summaries. These are
**derived analytics, not audit truth**: ``telemetry_records`` are
hash-chained and immutable, so a topic label CANNOT live on a
record's ``metadata`` — that would change its ``content_hash`` and
break the chain. Classifications live here, keyed by ``record_id``,
and can be re-run / re-versioned freely without ever touching the
chain.

One row per ``(tenant, record)``. The classify task (TM4 Track D)
writes a row when it labels an MCP record's ``summary`` into the
fixed taxonomy (see ``vargate_telemetry/topics/taxonomy.py``).
``taxonomy_version`` lets the taxonomy evolve without reinterpreting
old labels; ``model`` records which Claude model produced the label
(repro / audit).

``record_id`` is deliberately NOT a foreign key to
``telemetry_records``: classification is decoupled, derived
analytics, and records are immutable + never-deleted, so orphans
can't arise. The join happens at query time.

Per-tenant RLS, same pattern as every other tenant table.

Revision ID: 0021_create_interaction_topics
Revises: 0020_create_user_aliases
Create Date: 2026-05-29 19:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_create_interaction_topics"
down_revision: Union[str, None] = "0020_create_user_aliases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "interaction_topics",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The telemetry_records.id this classifies. NOT a FK (see
        # module docstring) — decoupled derived analytics over an
        # immutable, never-deleted chain table.
        sa.Column(
            "record_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        # The taxonomy category (see topics/taxonomy.py). Bounded
        # string, not an enum, so the taxonomy can evolve in code
        # without a DDL migration each time.
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("taxonomy_version", sa.String(16), nullable=False),
        # Which Claude model produced the label (e.g. a Haiku
        # version). Nullable so a future deterministic classifier
        # can leave it blank.
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column(
            "classified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # One classification per record per tenant. Re-classification
        # (e.g. a taxonomy bump) UPDATEs this row. Also indexes
        # record_id for the classify task's "records lacking a
        # classification" NOT EXISTS lookup.
        sa.UniqueConstraint(
            "tenant_id",
            "record_id",
            name="uq_interaction_topics_record",
        ),
    )
    # Aggregation index for the "Top topics" rollup (GROUP BY topic
    # per tenant).
    op.create_index(
        "idx_interaction_topics_tenant_topic",
        "interaction_topics",
        ["tenant_id", "topic"],
    )
    # RLS — same per-tenant pattern as every other tenant table.
    op.execute("ALTER TABLE interaction_topics ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE interaction_topics FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_interaction_topics ON interaction_topics
            USING (tenant_id::text = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_interaction_topics "
        "ON interaction_topics"
    )
    op.drop_index(
        "idx_interaction_topics_tenant_topic",
        table_name="interaction_topics",
    )
    op.drop_table("interaction_topics")
