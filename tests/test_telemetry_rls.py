# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Row-level-security smoke tests for T1.5.

These exercise the `_rls_canary` placeholder table. The three properties
under test are the contract every tenant-owned Telemetry table must
satisfy:

  - With `app.tenant_id` unset, SELECT returns zero rows.
  - With `app.tenant_id` = tenant-A, only tenant-A's rows are visible.
  - With `app.tenant_id` = tenant-B, tenant-A's rows are invisible.

We deliberately bypass `session_scope` here so we can drive the GUC
manually. session_scope is what production code uses; these tests prove
the underlying RLS layer would catch a bug in session_scope itself.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.fixture
def clean_canary() -> None:
    """Empty `_rls_canary` before and after each test.

    TRUNCATE is a privileged DDL operation and is not subject to RLS
    USING/WITH CHECK clauses, so it works without a tenant GUC set.
    """
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE _rls_canary RESTART IDENTITY"))
    yield
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE _rls_canary RESTART IDENTITY"))


def _insert_row(tenant: str, payload: str) -> None:
    """Insert a single row under the given tenant. Sets the GUC so WITH CHECK passes."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": tenant},
        )
        conn.execute(
            text("INSERT INTO _rls_canary (tenant_id, payload) VALUES (:t, :p)"),
            {"t": tenant, "p": payload},
        )


def test_rls_blocks_unset_tenant(clean_canary: None) -> None:
    """No tenant GUC set -> zero rows visible, even though tenant-A has rows."""
    from vargate_telemetry.db import engine

    _insert_row("tenant-A", "secret-A")

    # Fresh connection, transaction-local GUC cleared explicitly to be
    # explicit about the precondition.
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            count = conn.execute(text("SELECT count(*) FROM _rls_canary")).scalar()
            assert count == 0


def test_rls_allows_correct_tenant(clean_canary: None) -> None:
    """`app.tenant_id` = tenant-A -> tenant-A's rows are visible."""
    from vargate_telemetry.db import engine

    _insert_row("tenant-A", "secret-A")

    with engine.connect() as conn:
        with conn.begin():
            conn.execute(
                text("SELECT set_config('app.tenant_id', 'tenant-A', true)"),
            )
            count = conn.execute(text("SELECT count(*) FROM _rls_canary")).scalar()
            assert count == 1

            payload = conn.execute(
                text("SELECT payload FROM _rls_canary"),
            ).scalar()
            assert payload == "secret-A"


def test_rls_blocks_other_tenant(clean_canary: None) -> None:
    """`app.tenant_id` = tenant-B -> tenant-A's rows are invisible."""
    from vargate_telemetry.db import engine

    _insert_row("tenant-A", "secret-A")
    _insert_row("tenant-A", "secret-A-2")

    with engine.connect() as conn:
        with conn.begin():
            conn.execute(
                text("SELECT set_config('app.tenant_id', 'tenant-B', true)"),
            )
            count = conn.execute(text("SELECT count(*) FROM _rls_canary")).scalar()
            assert count == 0
