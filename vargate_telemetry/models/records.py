# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Telemetry record model (T2.1).

A `telemetry_records` row is one ingested Anthropic-side event. The
chain_* columns position the record in the per-tenant hash chain that
T2.2 wires up via the vargate-audit-chain package; T2.1 just defines
the shape and the integrity constraints.

Standard tenant_isolation policy + ENABLE + FORCE; see
docs/architecture/postgres-rls.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from vargate_telemetry.models.base import Base, TenantOwned


class TelemetryRecord(Base, TenantOwned):
    """One row per ingested Anthropic-side event."""

    __tablename__ = "telemetry_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    record_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_api: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    subject_user_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    content_ref: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True,
    )
    content_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # The SQL column is named `metadata` (per the schema spec); the
    # Python attribute can't be — SQLAlchemy's DeclarativeBase reserves
    # `metadata` on every model class for the schema-collection object.
    # `mapped_column("metadata", ...)` keeps the SQL name and exposes a
    # different Python name.
    record_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
    )

    chain_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chain_prev_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    chain_self_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_api",
            "external_id",
            name="uq_telemetry_records_dedup",
        ),
        UniqueConstraint(
            "tenant_id",
            "chain_seq",
            name="uq_telemetry_records_chain",
        ),
        Index(
            "ix_telemetry_records_tenant_occurred",
            "tenant_id",
            "occurred_at",
        ),
    )
