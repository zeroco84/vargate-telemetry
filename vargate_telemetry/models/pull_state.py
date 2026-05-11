# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Per-tenant per-source pull-cursor state (T3.4).

One row per `(tenant_id, source_api)` pair. The Celery pull task
reads `cursor` before each fetch and writes the advanced cursor
back on success — so an incremental pull never re-fetches and a
backfill is resumable across worker crashes.

Standard tenant_isolation policy + ENABLE + FORCE per
`docs/architecture/postgres-rls.md`. tenant_id is part of the
composite primary key so we drop the `TenantOwned` mixin's
secondary index — the PK already covers tenant_id lookups.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from vargate_telemetry.models.base import Base


class PullState(Base):
    """Cursor + last-status state for one (tenant, source_api) ingest stream."""

    __tablename__ = "pull_state"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_api: Mapped[str] = mapped_column(String(32), primary_key=True)
    cursor: Mapped[Optional[str]] = mapped_column(String(512))
    last_pulled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    last_status: Mapped[Optional[str]] = mapped_column(String(32))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
