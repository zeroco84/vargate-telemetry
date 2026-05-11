# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""User + Session models for the SSO-backed gateway (T4.2).

`users` is global — a user might have access to multiple tenants over
time (T4.5+ wires the binding). No RLS: rows are read by the
unauth'd SSO callback path that doesn't have a tenant context yet.
Access control is via the JWT carried in the `ogma_session` cookie,
which the FastAPI dependency `current_user` decodes per request.

`sessions` carries refresh-token state. T4.2 creates the schema but
leaves it unused — refresh-token issuance lands in a later task
(short-lived JWT access tokens are sufficient for the first
implementation). The schema is here now so the matching migration
doesn't churn.

Natural key on users: `(sso_provider, sso_subject_id)`. Email isn't
unique because the same person can have a Google account and a
Microsoft account with the same email; they're two distinct user
rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from vargate_telemetry.models.base import Base


class User(Base):
    """One row per (sso_provider, sso_subject_id) — Vargate user identity."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    sso_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    sso_subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )

    __table_args__ = (
        UniqueConstraint(
            "sso_provider",
            "sso_subject_id",
            name="uq_users_provider_subject",
        ),
    )


class Session(Base):
    """One row per active refresh token (T4.x consumer; T4.2 just creates the schema)."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
