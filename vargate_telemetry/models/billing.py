# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Ogma billing-wiring models — Stripe usage dispatch + retry queue (T2.4).

`tenant_billing` maps a tenant to its Stripe subscription item, the
target of `SubscriptionItem.create_usage_record` calls made by the
metering flush. T2.4 inserts rows from tests; T4 (onboarding) will be
the real producer.

`billing_retry` is a write-only failure queue. When a Stripe dispatch
raises mid-flush, the failed (tenant, bucket, record_type, quantity)
plus the error message land here under the same transaction as the
usage_records UPSERT, so we never lose a billable count to a transient
Stripe outage. A future worker drains it; for now ops monitor row
count and clear by hand.

Both tables follow the standard tenant_isolation policy + ENABLE +
FORCE pattern from docs/architecture/postgres-rls.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from vargate_telemetry.models.base import Base, TenantOwned


class TenantBilling(Base):
    """One row per billing-onboarded tenant. `tenant_id` is the PK and the
    RLS key — there is at most one Stripe subscription item per tenant.

    Does not inherit `TenantOwned` because the PK index already covers
    the tenant_id lookup; the mixin's secondary index would be redundant.
    """

    __tablename__ = "tenant_billing"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subscription_item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class BillingRetry(Base, TenantOwned):
    """One row per failed Stripe dispatch. Append-only from the flush path."""

    __tablename__ = "billing_retry"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    record_type: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
