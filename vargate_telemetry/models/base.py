# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""SQLAlchemy declarative base and the TenantOwned mixin used by every table."""

from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all Telemetry ORM models."""


class TenantOwned:
    """Mixin every Telemetry table inherits from.

    `tenant_id` must be the leading column on every composite index, and
    every RLS policy keys off this column. Models declare their other
    indexes explicitly; this mixin only contributes the column itself.
    """

    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
