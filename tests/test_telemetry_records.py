# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the telemetry_records schema (T2.1).

The chain_* columns are filled with stub values here; T2.2's tests
exercise the actual chain producer against vargate-audit-chain.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import select, text as sql_text
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def clean_records() -> None:
    """Empty telemetry_records before/after each test."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE"
            )
        )
    yield
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE"
            )
        )


def _make_record_kwargs(tenant_id: str, **overrides: Any) -> dict[str, Any]:
    """Build a kwargs dict suitable for `TelemetryRecord(**kwargs)`."""
    base: dict[str, Any] = {
        "tenant_id": tenant_id,
        "record_type": "usage",
        "source_api": "admin",
        "external_id": str(uuid.uuid4()),
        "subject_user_id": None,
        "occurred_at": datetime.now(timezone.utc),
        "content_ref": None,
        "content_hash": b"\x01" * 32,
        "record_metadata": {"tokens": 100, "model": "claude-opus-4-7"},
        "chain_seq": 1,
        "chain_prev_hash": b"\x00" * 32,
        "chain_self_hash": b"\x02" * 32,
    }
    base.update(overrides)
    return base


def test_telemetry_record_create(clean_records: None) -> None:
    """Insert one record, read back through session_scope, verify RLS scoping."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.models import TelemetryRecord

    tenant_a = "test-tenant-records-A"
    tenant_b = "test-tenant-records-B"

    payload = _make_record_kwargs(tenant_a)

    with session_scope(tenant_a) as s:
        s.add(TelemetryRecord(**payload))

    with session_scope(tenant_a) as s:
        rows = s.execute(select(TelemetryRecord)).scalars().all()
        assert len(rows) == 1
        assert rows[0].external_id == payload["external_id"]
        assert rows[0].record_metadata == {
            "tokens": 100,
            "model": "claude-opus-4-7",
        }
        assert rows[0].subject_user_id is None
        assert rows[0].content_ref is None

    # RLS: tenant B's session sees nothing.
    with session_scope(tenant_b) as s:
        rows = s.execute(select(TelemetryRecord)).scalars().all()
        assert rows == []


def test_dedup_unique_constraint(clean_records: None) -> None:
    """Two inserts with same (tenant_id, source_api, external_id) raise."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.models import TelemetryRecord

    tenant = "test-tenant-dedup"
    shared_external_id = "ext-id-shared"

    with session_scope(tenant) as s:
        s.add(
            TelemetryRecord(
                **_make_record_kwargs(
                    tenant,
                    external_id=shared_external_id,
                    chain_seq=1,
                )
            )
        )

    # Same dedup key, different chain_seq -> unique violation.
    with pytest.raises(IntegrityError):
        with session_scope(tenant) as s:
            s.add(
                TelemetryRecord(
                    **_make_record_kwargs(
                        tenant,
                        external_id=shared_external_id,
                        chain_seq=2,
                    )
                )
            )


def test_chain_seq_unique(clean_records: None) -> None:
    """Same chain_seq twice for one tenant raises."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.models import TelemetryRecord

    tenant = "test-tenant-chain"

    with session_scope(tenant) as s:
        s.add(
            TelemetryRecord(
                **_make_record_kwargs(
                    tenant,
                    external_id="ext-1",
                    chain_seq=42,
                )
            )
        )

    with pytest.raises(IntegrityError):
        with session_scope(tenant) as s:
            s.add(
                TelemetryRecord(
                    **_make_record_kwargs(
                        tenant,
                        external_id="ext-2",
                        chain_seq=42,
                    )
                )
            )
