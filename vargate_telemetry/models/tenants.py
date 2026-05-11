# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Scheduler-readable tenant index (T3.4).

`tenants` is the small global index the Celery beat scheduler reads
to enumerate active tenants and fan out per-tenant ingest tasks. It
has **NO RLS** — the only readers are:

  - `vargate_scheduler` (NOLOGIN), with explicit `SELECT` GRANT'd in
    migration `0009_create_tenants_index`
  - the bootstrap superuser, for migrations and ops

`vargate_app` does NOT have privileges on this table; the migration
explicitly REVOKEs the default GRANT that `0002_create_app_role`
would otherwise auto-apply.

The split — RLS-bypassed enumeration in one role, RLS-enforced
execution in another — is the standard answer for any future
cross-tenant enumeration need (billing roll-ups, fleet-wide health
metrics, etc.). See `docs/architecture/postgres-rls.md` for the
fuller convention.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from vargate_telemetry.models.base import Base


class Tenant(Base):
    """One row per provisioned tenant. Scheduler reads; vargate_app cannot."""

    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    region: Mapped[str] = mapped_column(String(8), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    billing_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default="trial",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
