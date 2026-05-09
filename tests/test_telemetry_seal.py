# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the seal / unseal flow on per-tenant DEKs (T1.7)."""

from __future__ import annotations

import subprocess

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import text


@pytest.fixture(scope="module", autouse=True)
def hsm_initialized() -> None:
    """Ensure SoftHSM2 token + KEK exist (shared with crypto tests)."""
    subprocess.run(
        ["python", "/app/scripts/init_telemetry_kek.py"],
        check=True,
    )


@pytest.fixture
def clean_secrets() -> None:
    """Empty tenant_deks and encrypted_secrets before/after each test.

    TRUNCATE bypasses RLS (DDL-style), so we don't need to set
    app.tenant_id. Running as the bootstrap superuser via engine.begin()
    is also outside the SET ROLE path used by app code.
    """
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            text("TRUNCATE TABLE encrypted_secrets RESTART IDENTITY CASCADE")
        )
        conn.execute(text("TRUNCATE TABLE tenant_deks RESTART IDENTITY CASCADE"))
    yield
    with engine.begin() as conn:
        conn.execute(
            text("TRUNCATE TABLE encrypted_secrets RESTART IDENTITY CASCADE")
        )
        conn.execute(text("TRUNCATE TABLE tenant_deks RESTART IDENTITY CASCADE"))


def test_dek_provision_idempotent(clean_secrets: None) -> None:
    """Calling provision_tenant_dek twice does not create a second row."""
    from vargate_telemetry.crypto.seal import provision_tenant_dek
    from vargate_telemetry.db import engine

    provision_tenant_dek("tenant-X")
    provision_tenant_dek("tenant-X")

    # Bypass RLS via the bootstrap user to count rows directly.
    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT count(*) FROM tenant_deks "
                "WHERE tenant_id = 'tenant-X'"
            )
        ).scalar()
    assert count == 1


def test_seal_unseal_roundtrip(clean_secrets: None) -> None:
    """Seal then unseal returns the original plaintext for the same tenant."""
    from vargate_telemetry.crypto.seal import (
        provision_tenant_dek,
        seal_secret,
        unseal_secret,
    )

    tenant = "tenant-A"
    provision_tenant_dek(tenant)

    plaintext = b"sk-ant-test-1234567890"
    seal_secret(tenant, "anthropic_admin_key", plaintext)
    assert unseal_secret(tenant, "anthropic_admin_key") == plaintext

    # Re-sealing under the same name rotates in place (UPSERT path).
    rotated = b"sk-ant-test-9999999999"
    seal_secret(tenant, "anthropic_admin_key", rotated)
    assert unseal_secret(tenant, "anthropic_admin_key") == rotated


def test_seal_other_tenant_invisible(clean_secrets: None) -> None:
    """Tenant A seals a secret; tenant B's session cannot read or list it."""
    from vargate_telemetry.crypto.seal import (
        provision_tenant_dek,
        seal_secret,
        unseal_secret,
    )

    provision_tenant_dek("tenant-A")
    provision_tenant_dek("tenant-B")

    seal_secret("tenant-A", "shared_name", b"A-secret")

    # Tenant B has no row matching the secret_name under its session;
    # RLS hides A's row, so the SELECT returns nothing and the API raises.
    with pytest.raises(LookupError):
        unseal_secret("tenant-B", "shared_name")

    # And tenant A's secret is still readable to tenant A.
    assert unseal_secret("tenant-A", "shared_name") == b"A-secret"


def test_seal_wrong_aad_fails(clean_secrets: None) -> None:
    """Tampering with the IV makes AES-GCM decryption raise InvalidTag."""
    from vargate_telemetry.crypto.seal import (
        provision_tenant_dek,
        seal_secret,
        unseal_secret,
    )
    from vargate_telemetry.db import engine

    provision_tenant_dek("tenant-A")
    seal_secret("tenant-A", "k", b"original")

    # Overwrite the IV with all-zero bytes via raw SQL (bypassing RLS via
    # the bootstrap superuser). This simulates an attacker tampering with
    # ciphertext at rest, or, equivalently, presenting wrong AAD on
    # decrypt — both surface as the same InvalidTag.
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE encrypted_secrets SET iv = :iv "
                "WHERE tenant_id = 'tenant-A' AND secret_name = 'k'"
            ),
            {"iv": b"\x00" * 12},
        )

    with pytest.raises(InvalidTag):
        unseal_secret("tenant-A", "k")
