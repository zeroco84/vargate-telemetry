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
        for tbl in (
            "encrypted_secrets",
            "tenant_deks",
            "telemetry_records",
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

    # Re-run: everything already present → nothing added (idempotent).
    r2 = demo_seed.seed_all(t)
    assert r2["content"]["added"] == 0
    assert r2["sessions"]["added"] == 0
    assert r2["usage"]["added"] == 0

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
