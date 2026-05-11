# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the pull-cursor table and the scheduler/app role split (T3.4)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.exc import ProgrammingError


@pytest.fixture
def clean_scheduler_state() -> None:
    """Empty pull_state + tenants before/after each test."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE pull_state, tenants RESTART IDENTITY CASCADE"
            )
        )

    yield

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE pull_state, tenants RESTART IDENTITY CASCADE"
            )
        )


# ----------------------------- pull_state --------------------------------


def test_cursor_advance(clean_scheduler_state: None) -> None:
    """UPSERT a cursor then read it back inside the same tenant scope."""
    from vargate_telemetry.db import session_scope

    tenant = "test-cursor-A"
    now = datetime.now(timezone.utc)

    with session_scope(tenant) as s:
        s.execute(
            sql_text(
                "INSERT INTO pull_state "
                "(tenant_id, source_api, cursor, last_pulled_at, last_status) "
                "VALUES (:t, 'admin', '2026-01-01T00:00:00Z', :ts, 'ok')"
            ),
            {"t": tenant, "ts": now},
        )

    with session_scope(tenant) as s:
        row = s.execute(
            sql_text(
                "SELECT cursor, last_status FROM pull_state "
                "WHERE tenant_id = :t AND source_api = 'admin'"
            ),
            {"t": tenant},
        ).first()

    assert row is not None
    assert row.cursor == "2026-01-01T00:00:00Z"
    assert row.last_status == "ok"


def test_cursor_rls_isolation(clean_scheduler_state: None) -> None:
    """Tenant A's cursor is invisible to a session scoped to tenant B."""
    from vargate_telemetry.db import session_scope

    tenant_a = "test-cursor-A"
    tenant_b = "test-cursor-B"

    with session_scope(tenant_a) as s:
        s.execute(
            sql_text(
                "INSERT INTO pull_state "
                "(tenant_id, source_api, cursor) "
                "VALUES (:t, 'admin', 'A-cursor')"
            ),
            {"t": tenant_a},
        )

    with session_scope(tenant_b) as s:
        row = s.execute(
            sql_text(
                "SELECT cursor FROM pull_state WHERE source_api = 'admin'"
            )
        ).first()
    assert row is None, (
        "RLS leak: tenant B saw tenant A's cursor"
    )

    # Sanity: tenant A still sees its own row.
    with session_scope(tenant_a) as s:
        row = s.execute(
            sql_text(
                "SELECT cursor FROM pull_state WHERE source_api = 'admin'"
            )
        ).first()
    assert row is not None
    assert row.cursor == "A-cursor"


# ---------------------- tenants index role split -------------------------


def test_tenants_index_visible_to_scheduler_role(
    clean_scheduler_state: None,
) -> None:
    """vargate_scheduler can SELECT active tenants from the `tenants` index."""
    from vargate_telemetry.db import engine, scheduler_session_scope

    # Seed three tenants from the bootstrap role (it has full GRANTs).
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, billing_status) VALUES "
                "('test-sched-1', 'us', true,  'paying'), "
                "('test-sched-2', 'eu', true,  'trial'), "
                "('test-sched-3', 'us', false, 'cancelled')"
            )
        )

    with scheduler_session_scope() as s:
        active_count = s.execute(
            sql_text("SELECT COUNT(*) FROM tenants WHERE active = true")
        ).scalar()
        # A peek at the actual rows — the scheduler will iterate these
        # to dispatch per-tenant Celery work.
        active_ids = [
            r[0]
            for r in s.execute(
                sql_text(
                    "SELECT tenant_id FROM tenants "
                    "WHERE active = true ORDER BY tenant_id"
                )
            )
        ]

    assert active_count == 2
    assert active_ids == ["test-sched-1", "test-sched-2"]


def test_tenants_index_invisible_to_app_role(
    clean_scheduler_state: None,
) -> None:
    """vargate_app has no GRANT on `tenants`; SELECT raises permission denied."""
    from vargate_telemetry.db import engine, session_scope

    # Even with a populated table, vargate_app cannot reach it.
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region) "
                "VALUES ('test-blocked', 'us')"
            )
        )

    with pytest.raises(ProgrammingError) as excinfo:
        with session_scope("test-blocked") as s:
            s.execute(sql_text("SELECT COUNT(*) FROM tenants")).scalar()

    msg = str(excinfo.value).lower()
    assert "permission denied" in msg and "tenants" in msg, (
        f"expected permission-denied on tenants, got: {excinfo.value}"
    )
