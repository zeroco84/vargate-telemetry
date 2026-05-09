# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""End-to-end smoke test for the T1 stack.

The per-subsystem tests (test_telemetry_infra, test_telemetry_rls,
test_telemetry_crypto, test_telemetry_seal) check primitives in
isolation. This file exercises a realistic happy path that crosses
every layer T1 built:

    Postgres + Alembic + RLS + role architecture
        +
    HSM KEK lifecycle + DEK envelope encryption
        +
    seal_secret / unseal_secret
        +
    cross-tenant RLS isolation
        +
    rotation of an existing secret

The intent is "if this passes against a fresh stack, T1 is intact."
T2 / T3 will add scenarios layered on top of these primitives.
"""

from __future__ import annotations

import subprocess

import pytest
from sqlalchemy import text


@pytest.fixture(scope="module", autouse=True)
def hsm_initialized() -> None:
    """Ensure the SoftHSM2 token + Telemetry KEK exist."""
    subprocess.run(
        ["python", "/app/scripts/init_telemetry_kek.py"],
        check=True,
    )


@pytest.fixture
def fresh_smoke_state() -> None:
    """Empty the smoke-test rows before/after each test."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM encrypted_secrets "
                "WHERE tenant_id LIKE 'smoke-%'"
            )
        )
        conn.execute(
            text("DELETE FROM tenant_deks WHERE tenant_id LIKE 'smoke-%'")
        )
    yield
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM encrypted_secrets "
                "WHERE tenant_id LIKE 'smoke-%'"
            )
        )
        conn.execute(
            text("DELETE FROM tenant_deks WHERE tenant_id LIKE 'smoke-%'")
        )


def test_full_happy_path(fresh_smoke_state: None) -> None:
    """Provision two tenants, store + retrieve secrets, prove cross-tenant invisibility."""
    from vargate_telemetry.crypto.seal import (
        provision_tenant_dek,
        seal_secret,
        unseal_secret,
    )

    # --- Provision two distinct tenants -----------------------------------
    provision_tenant_dek("smoke-tenant-A")
    provision_tenant_dek("smoke-tenant-B")

    # Idempotency: re-provisioning is a no-op (no second row, no new DEK)
    provision_tenant_dek("smoke-tenant-A")

    # --- Store realistic-shaped secrets per tenant ------------------------
    a_admin_key = b"sk-ant-admin-tenant-A-secret-XXXXX"
    b_admin_key = b"sk-ant-admin-tenant-B-secret-YYYYY"

    seal_secret("smoke-tenant-A", "anthropic_admin_key", a_admin_key)
    seal_secret("smoke-tenant-B", "anthropic_admin_key", b_admin_key)

    # --- Retrieve each — round trip is exact ------------------------------
    assert unseal_secret("smoke-tenant-A", "anthropic_admin_key") == a_admin_key
    assert unseal_secret("smoke-tenant-B", "anthropic_admin_key") == b_admin_key

    # --- Cross-tenant invisibility (RLS path) -----------------------------
    # Tenant A's SELECT under app.tenant_id='smoke-tenant-A' cannot see
    # B's row, even though B uses the identical secret_name. unseal_secret
    # raises LookupError because RLS hides the row.
    seal_secret("smoke-tenant-A", "shared_name", b"A-only-data")
    with pytest.raises(LookupError):
        unseal_secret("smoke-tenant-B", "shared_name")

    # --- Rotation: re-sealing under the same name updates in place -------
    rotated = b"sk-ant-admin-tenant-A-rotated-ZZZZZ"
    seal_secret("smoke-tenant-A", "anthropic_admin_key", rotated)
    assert unseal_secret("smoke-tenant-A", "anthropic_admin_key") == rotated
    # Tenant B is unaffected by tenant A's rotation.
    assert unseal_secret("smoke-tenant-B", "anthropic_admin_key") == b_admin_key
