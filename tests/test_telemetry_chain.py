# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the Ogma chain wrapper around vargate-audit-chain (T2.2).

Acceptance criteria from the T2.2 spec, mapped to tests below:

  - test_append_two_records_chain_verifies — happy-path append+verify.
  - test_append_concurrent_same_tenant — UNIQUE (tenant_id, chain_seq)
    serializes parallel appends; chain never forks.
  - test_chains_are_isolated_per_tenant — replaces the obsolete
    "test_existing_pro_records_unaffected" from earlier drafts of the
    spec. With the corrected two-chain model (Tyr's audit_log chain
    and Ogma's telemetry_records chain are parallel, never merged),
    the relevant invariant is per-tenant isolation, not cross-product
    interleaving.
  - test_verify_detects_tampering — bit-flipping metadata breaks the
    chain hash recomputation, so verify returns valid=False.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql_text


@pytest.fixture
def clean_records() -> None:
    """Empty telemetry_records before/after each chain test."""
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


def test_append_two_records_chain_verifies(clean_records: None) -> None:
    """Append two records, verify chain — both links pass."""
    from vargate_telemetry.chain import (
        append_telemetry_record,
        verify_telemetry_chain,
    )

    tenant = "test-chain-A"
    now = datetime.now(timezone.utc)

    r1 = append_telemetry_record(
        tenant_id=tenant,
        record_type="usage",
        source_api="admin",
        external_id="ext-1",
        occurred_at=now,
        content_hash=b"\x01" * 32,
        record_metadata={"tokens": 100},
    )
    r2 = append_telemetry_record(
        tenant_id=tenant,
        record_type="prompt",
        source_api="compliance",
        external_id="ext-2",
        occurred_at=now,
        content_hash=b"\x02" * 32,
        record_metadata={"model": "claude-opus-4-7"},
    )

    # Sequence advances correctly and the second record's prev_hash
    # points at the first record's self_hash (raw bytes equal).
    assert r1.chain_seq == 1
    assert r2.chain_seq == 2
    assert bytes(r2.chain_prev_hash) == bytes(r1.chain_self_hash)

    result = verify_telemetry_chain(tenant)
    assert result.valid is True
    assert result.record_count == 2
    assert result.failure_reason is None


def test_append_concurrent_same_tenant(clean_records: None) -> None:
    """Two parallel appends never produce a chain fork.

    Both threads call `append_telemetry_record` for the same tenant
    simultaneously. The UNIQUE (tenant_id, chain_seq) constraint
    forces serialization: either both threads succeed (one wins the
    chain_seq=1 slot, the other gets chain_seq=2), or one fails with
    IntegrityError. Either way, the resulting chain has no duplicate
    chain_seq values and no gaps.
    """
    from sqlalchemy.exc import IntegrityError

    from vargate_telemetry.chain import (
        append_telemetry_record,
        verify_telemetry_chain,
    )
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.models import TelemetryRecord

    tenant = "test-chain-concurrent"
    now = datetime.now(timezone.utc)

    results: dict[str, tuple[str, object]] = {}

    def try_append(label: str, ext_id: str) -> None:
        try:
            rec = append_telemetry_record(
                tenant_id=tenant,
                record_type="usage",
                source_api="admin",
                external_id=ext_id,
                occurred_at=now,
                content_hash=b"\x01" * 32,
            )
            results[label] = ("ok", rec.chain_seq)
        except IntegrityError:
            results[label] = ("integrity_error", None)

    threads = [
        threading.Thread(target=try_append, args=("a", "ext-a")),
        threading.Thread(target=try_append, args=("b", "ext-b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Read every chain_seq for this tenant and assert no forks and no gaps.
    with session_scope(tenant) as s:
        seqs = (
            s.execute(
                select(TelemetryRecord.chain_seq).order_by(
                    TelemetryRecord.chain_seq.asc()
                )
            )
            .scalars()
            .all()
        )
    assert seqs == list(range(1, len(seqs) + 1)), (
        f"chain forked or has gaps; got chain_seqs={seqs}"
    )

    # And whatever did land verifies cleanly.
    result = verify_telemetry_chain(tenant)
    assert result.valid is True


def test_chains_are_isolated_per_tenant(clean_records: None) -> None:
    """One tenant's chain never touches another's — independent genesis.

    Replaces the obsolete `test_existing_pro_records_unaffected` from
    early T2.2 drafts. Per the corrected two-chain architecture
    (ADR-001 amendment, 2026-05-10), each tenant has its own chain
    over its own `telemetry_records`, and Tyr's audit_log chain is
    fully separate. This test exercises the per-tenant boundary.
    """
    from vargate_telemetry.chain import (
        append_telemetry_record,
        verify_telemetry_chain,
    )

    now = datetime.now(timezone.utc)

    a1 = append_telemetry_record(
        tenant_id="tenant-A",
        record_type="usage",
        source_api="admin",
        external_id="A-1",
        occurred_at=now,
        content_hash=b"\x01" * 32,
    )
    b1 = append_telemetry_record(
        tenant_id="tenant-B",
        record_type="usage",
        source_api="admin",
        external_id="B-1",
        occurred_at=now,
        content_hash=b"\x02" * 32,
    )
    a2 = append_telemetry_record(
        tenant_id="tenant-A",
        record_type="prompt",
        source_api="compliance",
        external_id="A-2",
        occurred_at=now,
        content_hash=b"\x03" * 32,
    )

    # Each tenant starts at chain_seq=1: independent genesis per chain.
    assert a1.chain_seq == 1
    assert b1.chain_seq == 1
    # Tenant A's second record is chain_seq=2 (not 3 — B doesn't bump A).
    assert a2.chain_seq == 2

    # Tenant A's chain prev_hash for record 2 points to A's record 1,
    # never B's record 1.
    assert bytes(a2.chain_prev_hash) == bytes(a1.chain_self_hash)
    assert bytes(a2.chain_prev_hash) != bytes(b1.chain_self_hash)

    # Both chains verify independently.
    res_a = verify_telemetry_chain("tenant-A")
    res_b = verify_telemetry_chain("tenant-B")
    assert res_a.valid is True and res_a.record_count == 2
    assert res_b.valid is True and res_b.record_count == 1


def test_verify_detects_tampering(clean_records: None) -> None:
    """Tampering with stored metadata breaks chain verification.

    The chain hash binds the metadata into chain_self_hash. Modifying
    the row's metadata in place (without re-hashing) means the stored
    chain_self_hash no longer matches the recomputed one.
    """
    from vargate_telemetry.chain import (
        append_telemetry_record,
        verify_telemetry_chain,
    )
    from vargate_telemetry.db import engine

    tenant = "test-chain-tamper"
    now = datetime.now(timezone.utc)

    append_telemetry_record(
        tenant_id=tenant,
        record_type="usage",
        source_api="admin",
        external_id="ext-1",
        occurred_at=now,
        content_hash=b"\x01" * 32,
        record_metadata={"tokens": 100},
    )

    # Tamper directly: change metadata without touching chain_self_hash.
    # Bypassing RLS via the bootstrap superuser is fine here — this is
    # a test simulating an attacker with raw DB access.
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "UPDATE telemetry_records SET metadata = :m "
                "WHERE tenant_id = :t"
            ),
            {"m": '{"tokens": 999}', "t": tenant},
        )

    result = verify_telemetry_chain(tenant)
    assert result.valid is False
    assert result.failed_at_index == 0
    assert "record_hash mismatch" in (result.failure_reason or "")
