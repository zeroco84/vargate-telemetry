# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the per-tenant Anthropic Admin client factory (T3.3)."""

from __future__ import annotations

import pytest
from sqlalchemy import text as sql_text


@pytest.fixture
def clean_factory_state() -> None:
    """Empty tenant_deks + encrypted_secrets before/after each test."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE encrypted_secrets, tenant_deks "
                "RESTART IDENTITY CASCADE"
            )
        )

    yield

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE encrypted_secrets, tenant_deks "
                "RESTART IDENTITY CASCADE"
            )
        )


def test_factory_uses_correct_tenant_key(clean_factory_state: None) -> None:
    """Two tenants → two distinct clients, each carrying its own admin key."""
    from vargate_telemetry.anthropic.factory import (
        ANTHROPIC_ADMIN_KEY_SECRET,
        admin_client_for_tenant,
    )
    from vargate_telemetry.crypto.seal import (
        provision_tenant_dek,
        seal_secret,
    )

    tenant_a = "test-factory-A"
    tenant_b = "test-factory-B"
    key_a = b"sk-ant-admin-AAA"
    key_b = b"sk-ant-admin-BBB"

    for tenant, key in [(tenant_a, key_a), (tenant_b, key_b)]:
        provision_tenant_dek(tenant)
        seal_secret(tenant, ANTHROPIC_ADMIN_KEY_SECRET, key)

    with admin_client_for_tenant(tenant_a) as client_a:
        with admin_client_for_tenant(tenant_b) as client_b:
            assert (
                client_a._client.headers["x-api-key"] == "sk-ant-admin-AAA"
            )
            assert (
                client_b._client.headers["x-api-key"] == "sk-ant-admin-BBB"
            )
            # The two clients hold genuinely different state — not the
            # same instance under a different binding.
            assert client_a is not client_b


def test_factory_missing_key_raises(clean_factory_state: None) -> None:
    """Tenant with DEK but no sealed admin key raises a descriptive LookupError."""
    from vargate_telemetry.anthropic.factory import admin_client_for_tenant
    from vargate_telemetry.crypto.seal import provision_tenant_dek

    tenant = "test-factory-missing"
    provision_tenant_dek(tenant)
    # Deliberately skip seal_secret — the admin key is absent.

    with pytest.raises(LookupError) as excinfo:
        admin_client_for_tenant(tenant)

    msg = str(excinfo.value)
    assert "anthropic_admin_key" in msg
    assert tenant in msg
