# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""ORM models for tenant_deks and encrypted_secrets (T1.7).

Both tables live behind RLS and the standard tenant_isolation policy;
see docs/architecture/postgres-rls.md. Application code reaches them
through `vargate_telemetry.crypto.seal` rather than directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from vargate_telemetry.models.base import Base, TenantOwned


class TenantDek(Base):
    """One row per tenant; wrapped_dek is the HSM-wrapped per-tenant DEK."""

    __tablename__ = "tenant_deks"

    # tenant_id is both the PK and the RLS column. Cannot inherit
    # TenantOwned because that mixin declares tenant_id as an indexed
    # non-PK column; here it's the primary key.
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kek_label: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class EncryptedSecret(Base, TenantOwned):
    """Per-tenant keyed secrets, each AES-GCM-encrypted under the tenant DEK."""

    __tablename__ = "encrypted_secrets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    secret_name: Mapped[str] = mapped_column(String(128), nullable=False)
    iv: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_rotated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "secret_name",
            name="uq_encrypted_secrets_tenant_name",
        ),
    )
