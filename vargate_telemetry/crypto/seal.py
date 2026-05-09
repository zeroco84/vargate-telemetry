# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""High-level seal / unseal API on per-tenant DEKs (T1.7).

Public surface:

  - `provision_tenant_dek(tenant_id)` — generate a DEK, wrap with the
    HSM KEK, store in tenant_deks. Idempotent.
  - `get_tenant_dek(tenant_id)` — return the unwrapped DEK bytes for use
    by callers that need to encrypt / decrypt many things in one go.
    The returned bytes are in-memory only; do not persist them.
  - `seal_secret(tenant_id, name, plaintext)` — AES-GCM-encrypt under
    the tenant DEK and UPSERT into encrypted_secrets.
  - `unseal_secret(tenant_id, name)` — look up, decrypt, return.

AAD binds every ciphertext to (tenant_id, secret_name). A wrapped row
moved to a different tenant_id, or renamed, fails decryption — this is
a small bit of belt-and-braces over what RLS already enforces.

Each public function opens its own `session_scope`, which:
  - SETs ROLE vargate_app (RLS applies)
  - SETs app.tenant_id GUC (the RLS policy reads this)
  - rolls back on exception, commits on clean exit

Callers MUST NOT hold the unwrapped DEK across long-running operations;
HSM-bound key material should be re-fetched per logical operation. T1.8
benchmarks the per-call HSM cost; if it dominates, T1.x+ may add an LRU
cache.
"""

from __future__ import annotations

import os

from sqlalchemy import func, select

from vargate_telemetry.crypto.dek import (
    decrypt_with_dek,
    encrypt_with_dek,
    generate_dek,
)
from vargate_telemetry.crypto.hsm import unwrap_dek, wrap_dek
from vargate_telemetry.db import session_scope
from vargate_telemetry.models.secrets import EncryptedSecret, TenantDek


def _aad_for_secret(tenant_id: str, secret_name: str) -> bytes:
    """AAD that binds a ciphertext to its (tenant_id, secret_name) pair."""
    return f"vargate.telemetry/secret/{tenant_id}/{secret_name}".encode("utf-8")


def provision_tenant_dek(tenant_id: str) -> None:
    """Generate a DEK for the tenant, wrap with the KEK, store. Idempotent."""
    if not tenant_id:
        raise ValueError("tenant_id required")

    kek_label = os.environ["HSM_KEK_LABEL"]

    with session_scope(tenant_id) as s:
        if s.get(TenantDek, tenant_id) is not None:
            return

        dek = generate_dek()
        wrapped = wrap_dek(dek)

        s.add(
            TenantDek(
                tenant_id=tenant_id,
                wrapped_dek=wrapped,
                kek_label=kek_label,
            )
        )


def get_tenant_dek(tenant_id: str) -> bytes:
    """Return the unwrapped 32-byte DEK for the tenant (in-memory only)."""
    if not tenant_id:
        raise ValueError("tenant_id required")

    with session_scope(tenant_id) as s:
        td = s.get(TenantDek, tenant_id)
        if td is None:
            raise LookupError(f"no DEK provisioned for tenant {tenant_id!r}")
        return unwrap_dek(bytes(td.wrapped_dek))


def seal_secret(tenant_id: str, name: str, plaintext: bytes) -> None:
    """Encrypt plaintext under the tenant DEK and UPSERT into encrypted_secrets."""
    if not tenant_id:
        raise ValueError("tenant_id required")
    if not name:
        raise ValueError("secret name required")

    aad = _aad_for_secret(tenant_id, name)

    with session_scope(tenant_id) as s:
        td = s.get(TenantDek, tenant_id)
        if td is None:
            raise LookupError(
                f"no DEK provisioned for tenant {tenant_id!r}; "
                "call provision_tenant_dek first"
            )
        dek = unwrap_dek(bytes(td.wrapped_dek))

        iv, ciphertext = encrypt_with_dek(dek, plaintext, aad=aad)

        existing = s.execute(
            select(EncryptedSecret).where(
                EncryptedSecret.secret_name == name,
            )
        ).scalar_one_or_none()

        if existing is not None:
            existing.iv = iv
            existing.ciphertext = ciphertext
            existing.last_rotated_at = func.now()
        else:
            s.add(
                EncryptedSecret(
                    tenant_id=tenant_id,
                    secret_name=name,
                    iv=iv,
                    ciphertext=ciphertext,
                )
            )


def unseal_secret(tenant_id: str, name: str) -> bytes:
    """Return the plaintext for (tenant_id, name), decrypted with the tenant DEK."""
    if not tenant_id:
        raise ValueError("tenant_id required")
    if not name:
        raise ValueError("secret name required")

    aad = _aad_for_secret(tenant_id, name)

    with session_scope(tenant_id) as s:
        td = s.get(TenantDek, tenant_id)
        if td is None:
            raise LookupError(f"no DEK provisioned for tenant {tenant_id!r}")
        dek = unwrap_dek(bytes(td.wrapped_dek))

        record = s.execute(
            select(EncryptedSecret).where(
                EncryptedSecret.secret_name == name,
            )
        ).scalar_one_or_none()

        if record is None:
            raise LookupError(
                f"no secret named {name!r} for tenant {tenant_id!r}"
            )

        return decrypt_with_dek(
            dek,
            bytes(record.iv),
            bytes(record.ciphertext),
            aad=aad,
        )
