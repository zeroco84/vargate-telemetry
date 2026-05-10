# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the Redis-counter + Postgres-flush metering pipeline (T2.3)."""

from __future__ import annotations

import threading

import pytest
import redis
from sqlalchemy import text as sql_text


@pytest.fixture
def clean_meter_state() -> None:
    """Empty Redis meter keys and usage_records before/after each test."""
    from vargate_telemetry.db import engine
    from vargate_telemetry.metering import _redis

    r = _redis()
    # Drop every meter key — active hash, any orphan flush hashes.
    for key in r.scan_iter("vargate:meter:*"):
        r.delete(key)

    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE usage_records RESTART IDENTITY CASCADE")
        )

    yield

    for key in r.scan_iter("vargate:meter:*"):
        r.delete(key)
    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE usage_records RESTART IDENTITY CASCADE")
        )


def test_increment_then_flush(clean_meter_state: None) -> None:
    """100 increments for one tenant land in Postgres as record_count=100."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.metering import flush, increment
    from vargate_telemetry.models import UsageRecord

    tenant = "test-metering-A"

    for _ in range(100):
        increment(tenant, "usage")

    processed = flush()
    # One (tenant, record_type, bucket) combination — all 100 increments
    # land in the same minute bucket.
    assert processed == 1

    with session_scope(tenant) as s:
        row = s.execute(
            sql_text(
                "SELECT record_count FROM usage_records "
                "WHERE tenant_id = :t AND record_type = 'usage'"
            ),
            {"t": tenant},
        ).first()

    assert row is not None
    assert row.record_count == 100

    # A second flush with no new increments is a no-op.
    assert flush() == 0


def test_concurrent_flush_safe(clean_meter_state: None) -> None:
    """Two threads calling flush() simultaneously do not double-count.

    The RENAME in Redis is atomic; one thread captures the active
    hash, the other gets ResponseError (active no longer exists) and
    returns 0. Combined, exactly the original increment total lands
    in Postgres.
    """
    from vargate_telemetry.metering import flush, increment

    tenant = "test-metering-concurrent"

    for _ in range(50):
        increment(tenant, "usage")

    results: list[int] = []

    def do_flush() -> None:
        results.append(flush())

    threads = [threading.Thread(target=do_flush) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # One thread processed everything (1 row of grouped fields), the
    # other found nothing to flush (0).
    assert sorted(results) == [0, 1]

    # Postgres total equals the original increment count — no
    # double-count from the race.
    from vargate_telemetry.db import engine

    with engine.connect() as conn:
        total = conn.execute(
            sql_text(
                "SELECT SUM(record_count) FROM usage_records WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).scalar()
    assert total == 50


def test_atomic_rename(clean_meter_state: None) -> None:
    """A failing UPSERT mid-flush leaves Postgres with no rows for that tenant.

    Setup: one valid increment plus a manually-injected Redis field
    whose record_type exceeds the column's varchar(32) — Postgres
    will reject the second UPSERT with StringDataRightTruncation,
    and SQLAlchemy will roll back the entire per-tenant transaction.
    """
    from sqlalchemy.exc import DataError

    from vargate_telemetry.db import engine
    from vargate_telemetry.metering import (
        _ACTIVE_HASH,
        _FIELD_DELIM,
        _minute_bucket,
        _redis,
        flush,
        increment,
    )

    tenant = "test-metering-atomic"

    # One legitimate counter — would succeed on its own.
    increment(tenant, "usage")

    # Inject a poison field: record_type of 50 chars busts varchar(32).
    r = _redis()
    bad_field = (
        f"{tenant}{_FIELD_DELIM}{'x' * 50}{_FIELD_DELIM}"
        f"{_minute_bucket().isoformat()}"
    )
    r.hset(_ACTIVE_HASH, bad_field, 7)

    # flush() raises during the second UPSERT; the per-tenant
    # transaction rolls back so the legitimate row never lands.
    with pytest.raises(DataError):
        flush()

    with engine.connect() as conn:
        row_count = conn.execute(
            sql_text(
                "SELECT COUNT(*) FROM usage_records WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).scalar()
    assert row_count == 0, (
        "atomicity broken: per-tenant batch should have rolled back"
    )


def test_flush_scheduled_in_beat() -> None:
    """The metering flush task is registered in celery_app's beat_schedule."""
    from vargate_telemetry.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert "flush-meter-counters" in schedule
    entry = schedule["flush-meter-counters"]
    assert entry["task"] == "vargate_telemetry.tasks.metering.flush_counters"
    assert entry["schedule"] == 60.0
