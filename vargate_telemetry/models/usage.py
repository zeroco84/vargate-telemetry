# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Ogma usage_records model — durable per-tenant counts (T2.3).

`usage_records` is the canonical source for billing. Every successful
ingest into `telemetry_records` increments a Redis counter (see
`vargate_telemetry.metering.increment`); a Celery beat task drains
those counters into this table once a minute via per-tenant UPSERT.

Standard tenant_isolation policy + ENABLE + FORCE per
`docs/architecture/postgres-rls.md`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from vargate_telemetry.models.base import Base, TenantOwned


class UsageRecord(Base, TenantOwned):
    """One row per (tenant, minute bucket, record_type) — billable count."""

    __tablename__ = "usage_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    record_type: Mapped[str] = mapped_column(String(32), nullable=False)
    record_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "bucket_start",
            "record_type",
            name="uq_usage_records_tenant_bucket_type",
        ),
    )
