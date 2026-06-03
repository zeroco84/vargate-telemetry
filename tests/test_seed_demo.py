# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the demo-seed (TM6 T6.S). Runs against the isolated `_test`
DB (conftest URL rewrite) so it never pollutes a real tenant's chain."""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from sqlalchemy import text as sql_text


@pytest.fixture
def seed_tenant() -> Iterator[str]:
    from vargate_telemetry.db import engine

    tid = f"tnt_us_seed_{uuid.uuid4().hex[:12]}"
    yield tid
    with engine.begin() as conn:
        # FK-safe order: children before parents (events→budgets→tenants;
        # users→tenants). Covers the volume seed's users + budgets too.
        for tbl in (
            "budget_alert_events",
            "budgets",
            "encrypted_secrets",
            "tenant_deks",
            "telemetry_records",
            "workspaces",
            "users",
            "tenants",
        ):
            conn.execute(
                sql_text(f"DELETE FROM {tbl} WHERE tenant_id = :t"), {"t": tid}
            )


def _count(tenant_id: str, where: str) -> int:
    from vargate_telemetry.db import session_scope

    with session_scope(tenant_id) as s:
        return s.execute(
            sql_text(
                "SELECT count(*) FROM telemetry_records "
                "WHERE tenant_id = :t AND " + where
            ),
            {"t": tenant_id},
        ).scalar_one()


def test_seed_all_idempotent_and_chain_valid(seed_tenant: str) -> None:
    from vargate_telemetry import demo_seed
    from vargate_telemetry.chain import verify_telemetry_chain

    t = seed_tenant
    r1 = demo_seed.seed_all(t)
    assert r1["content"]["added"] == 7  # 3 + 2 + 2 messages
    assert r1["sessions"]["added"] == 6
    assert r1["usage"]["added"] == 3
    assert r1["budgets"]["added"] == 1

    # Re-run: everything already present → nothing added (idempotent).
    r2 = demo_seed.seed_all(t)
    assert r2["content"]["added"] == 0
    assert r2["sessions"]["added"] == 0
    assert r2["usage"]["added"] == 0
    assert r2["budgets"]["added"] == 0

    # Chain verifies after seeding — incl. the demo deletion event.
    assert verify_telemetry_chain(t).valid is True


def test_seed_populates_each_surface(seed_tenant: str) -> None:
    from vargate_telemetry import demo_seed

    t = seed_tenant
    demo_seed.seed_all(t)

    # Content messages all remain (deletion never removes records).
    assert (
        _count(t, "source_api = 'compliance_content' AND record_type = 'chat_message'")
        == 7
    )
    # The demo chat was deleted → one content_deletion event per message.
    assert _count(t, "record_type = 'content_deletion'") == 2
    # Sessions surfaces.
    assert (
        _count(t, "source_api IN ('code_analytics', 'compliance_activities')")
        == 6
    )
    # Usage rows.
    assert _count(t, "record_type = 'usage' AND source_api = 'admin'") == 3

    # A budget + one fired alert event (Budgets + /alerts surfaces).
    from vargate_telemetry.db import session_scope

    with session_scope(t) as s:
        budgets = s.execute(
            sql_text(
                "SELECT count(*) FROM budgets WHERE tenant_id = :t "
                "AND deleted_at IS NULL"
            ),
            {"t": t},
        ).scalar_one()
        alerts = s.execute(
            sql_text(
                "SELECT count(*) FROM budget_alert_events WHERE tenant_id = :t"
            ),
            {"t": t},
        ).scalar_one()
    assert budgets == 1
    assert alerts == 1


def test_seed_volume_users_activity_and_chain(seed_tenant: str) -> None:
    from vargate_telemetry import demo_seed
    from vargate_telemetry.chain import verify_telemetry_chain
    from vargate_telemetry.db import session_scope

    t = seed_tenant
    r1 = demo_seed.seed_volume(t, days=3)
    assert r1["users_added"] == 16
    assert r1["events_added"] > 0
    assert r1["usage_added"] == 9  # 3 days × 3 models
    assert r1["content_added"] > 0

    # Idempotent: deterministic rng + absolute-date external_ids.
    r2 = demo_seed.seed_volume(t, days=3)
    assert r2 == {
        "users_added": 0,
        "events_added": 0,
        "usage_added": 0,
        "content_added": 0,
    }

    # Chain stays valid after the bulk seed + the demo deletions.
    assert verify_telemetry_chain(t).valid is True

    with session_scope(t) as s:
        users = s.execute(
            sql_text("SELECT count(*) FROM users WHERE tenant_id = :t"),
            {"t": t},
        ).scalar_one()
        mcp = s.execute(
            sql_text(
                "SELECT count(*) FROM telemetry_records WHERE tenant_id = :t "
                "AND source_api = 'mcp'"
            ),
            {"t": t},
        ).scalar_one()
    assert users == 16  # the roster stitches into the Users dashboard
    assert mcp > 0
