# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Ogma-side wrapper over the shared vargate-audit-chain primitives (T2.2).

Ogma's per-tenant hash chain lives in the `telemetry_records` table.
This module is the thin bridge that:

  - reads the chain tip for a tenant,
  - builds canonical bytes from a record's immutable fields,
  - calls `vargate_audit_chain.compute_record_hash` for the tenant-bound
    SHA-256 digest,
  - writes the new row with `chain_seq`, `chain_prev_hash`, and
    `chain_self_hash` populated.

The shared package handles only the math; storage stays here because
Ogma's `telemetry_records` schema is its own concern.

This is a parallel chain to Tyr's `audit_log` chain — same crypto,
separate sequences, separate genesis per tenant per product. See
`docs/architecture/postgres-rls.md` for RLS conventions and the
package's README for the chain-hash contract.

Storage convention for the GENESIS edge:
  - `chain_self_hash` is the raw 32 bytes of the SHA-256 digest from
    `compute_record_hash`. Stored as bytea(32).
  - `chain_prev_hash` for chain_seq >= 2 is the previous record's
    `chain_self_hash`. Stored as bytea(32).
  - `chain_prev_hash` for chain_seq == 1 has no predecessor; the column
    is bytea(32) NOT NULL so we store the deterministic sentinel
    `SHA-256("vargate.telemetry/chain/genesis")`. The actual call to
    `compute_record_hash` for that record passes the string
    `GENESIS_HASH` ("GENESIS") as `prev_hash` — never the sentinel
    bytes — so the math matches the package contract.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Iterator

from sqlalchemy import select
from vargate_audit_chain import (
    GENESIS_HASH,
    ChainRecord,
    VerifyResult,
    compute_record_hash,
    verify_record_chain,
)

from vargate_telemetry.db import session_scope
from vargate_telemetry.models.records import TelemetryRecord

# Deterministic 32-byte sentinel stored in `chain_prev_hash` for the
# first record in any tenant's chain. NOT passed to compute_record_hash
# — that always uses the string `GENESIS_HASH` for the first record.
GENESIS_PREV_BYTES: bytes = hashlib.sha256(
    b"vargate.telemetry/chain/genesis"
).digest()


def _canonical_dict(
    *,
    record_type: str,
    source_api: str,
    external_id: str,
    subject_user_id: str | None,
    occurred_at: datetime,
    content_ref: str | None,
    content_hash: bytes,
    record_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build the immutable dict that goes into chain hash.

    Includes everything that defines the record's *content* and excludes
    operational metadata (id, ingested_at, tenant_id, chain_*). The
    tenant_id is bound by `compute_record_hash` itself; the chain_*
    columns are *derived from* this dict, so including them would be
    circular.
    """
    return {
        "content_hash": content_hash.hex(),
        "content_ref": content_ref,
        "external_id": external_id,
        "metadata": record_metadata,
        "occurred_at": (
            occurred_at.isoformat()
            if isinstance(occurred_at, datetime)
            else occurred_at
        ),
        "record_type": record_type,
        "source_api": source_api,
        "subject_user_id": subject_user_id,
    }


def _canonical_bytes(**fields: Any) -> bytes:
    """Serialize a canonical dict as deterministic JSON bytes."""
    return json.dumps(
        _canonical_dict(**fields),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def append_telemetry_record(
    tenant_id: str,
    *,
    record_type: str,
    source_api: str,
    external_id: str,
    occurred_at: datetime,
    content_hash: bytes,
    record_metadata: dict[str, Any] | None = None,
    subject_user_id: str | None = None,
    content_ref: str | None = None,
) -> TelemetryRecord:
    """Append a record to the tenant's chain; returns the persisted row.

    Inside a single `session_scope(tenant_id)` transaction:
      1. Find the chain tip (max chain_seq for this tenant) and read
         its chain_self_hash to use as this record's prev.
      2. Build canonical bytes from the record fields.
      3. Compute `chain_self_hash` via the shared primitive.
      4. INSERT with `chain_seq`, `chain_prev_hash`, `chain_self_hash`.

    Concurrency: the UNIQUE (tenant_id, chain_seq) constraint serializes
    parallel appends. If two transactions race, one commits and the
    other raises `IntegrityError`; the caller may retry.
    """
    if not tenant_id:
        raise ValueError("tenant_id required")
    if len(content_hash) != 32:
        raise ValueError(
            f"content_hash must be 32 bytes (got {len(content_hash)})"
        )

    metadata = record_metadata or {}

    with session_scope(tenant_id) as s:
        tip = s.execute(
            select(
                TelemetryRecord.chain_seq,
                TelemetryRecord.chain_self_hash,
            )
            .order_by(TelemetryRecord.chain_seq.desc())
            .limit(1)
        ).first()

        if tip is None:
            chain_seq = 1
            prev_hash_str = GENESIS_HASH
            chain_prev_bytes = GENESIS_PREV_BYTES
        else:
            chain_seq = tip.chain_seq + 1
            chain_prev_bytes = bytes(tip.chain_self_hash)
            prev_hash_str = chain_prev_bytes.hex()

        canonical = _canonical_bytes(
            record_type=record_type,
            source_api=source_api,
            external_id=external_id,
            subject_user_id=subject_user_id,
            occurred_at=occurred_at,
            content_ref=content_ref,
            content_hash=content_hash,
            record_metadata=metadata,
        )

        self_hash_hex = compute_record_hash(tenant_id, canonical, prev_hash_str)
        chain_self_bytes = bytes.fromhex(self_hash_hex)

        record = TelemetryRecord(
            tenant_id=tenant_id,
            record_type=record_type,
            source_api=source_api,
            external_id=external_id,
            subject_user_id=subject_user_id,
            occurred_at=occurred_at,
            content_ref=content_ref,
            content_hash=content_hash,
            record_metadata=metadata,
            chain_seq=chain_seq,
            chain_prev_hash=chain_prev_bytes,
            chain_self_hash=chain_self_bytes,
        )
        s.add(record)
        s.flush()
        s.refresh(record)
        # Detach so the returned ORM instance remains usable after the
        # session closes on `with` exit.
        s.expunge(record)
        return record


def verify_telemetry_chain(tenant_id: str) -> VerifyResult:
    """Walk the tenant's chain in chain_seq order and verify integrity.

    Loads every record under RLS scope, builds a `ChainRecord` per row,
    and delegates to `verify_record_chain`. Returns
    `VerifyResult(valid=True, record_count=N)` on success.
    """
    if not tenant_id:
        raise ValueError("tenant_id required")

    with session_scope(tenant_id) as s:
        rows = (
            s.execute(
                select(TelemetryRecord).order_by(
                    TelemetryRecord.chain_seq.asc()
                )
            )
            .scalars()
            .all()
        )
        # Materialize the chain-record view inside the session so the
        # row attributes are loaded before the session closes.
        materialized: list[ChainRecord] = [
            _row_to_chain_record(row) for row in rows
        ]

    return verify_record_chain(tenant_id, iter(materialized))


def _row_to_chain_record(row: TelemetryRecord) -> ChainRecord:
    """Convert a TelemetryRecord row to the package's `ChainRecord`."""
    canonical = _canonical_bytes(
        record_type=row.record_type,
        source_api=row.source_api,
        external_id=row.external_id,
        subject_user_id=row.subject_user_id,
        occurred_at=row.occurred_at,
        content_ref=row.content_ref,
        content_hash=bytes(row.content_hash),
        record_metadata=row.record_metadata,
    )
    # For chain_seq=1 the original compute_record_hash call used the
    # string GENESIS_HASH; the stored bytes (GENESIS_PREV_BYTES) are a
    # storage convenience, not the hash input.
    prev_str = (
        GENESIS_HASH
        if row.chain_seq == 1
        else bytes(row.chain_prev_hash).hex()
    )
    return ChainRecord(
        canonical_bytes=canonical,
        prev_hash=prev_str,
        record_hash=bytes(row.chain_self_hash).hex(),
    )
