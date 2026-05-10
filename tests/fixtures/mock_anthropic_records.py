# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Synthetic Anthropic-shape ingest records for the T2 end-to-end test.

The shape mirrors what a real Anthropic Admin API row will normalize
to after T3.5's pull task lands: a record_type, a source_api tag, an
external_id for dedup, an occurred_at timestamp, a content blob that
gets sealed under the tenant DEK, and a precomputed SHA-256 over the
content that the chain hashes.

`generate_mock_records` is the only public entry point; tests pass it
the desired count and (optionally) the record_types to round-robin
across. Default record_types are `usage` and `prompt` — the two we
expect dominant in v1 ingest volumes.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator


@dataclass(frozen=True)
class MockRecord:
    """One synthetic ingest payload. Immutable so tests can hash it freely."""

    record_type: str
    source_api: str
    external_id: str
    occurred_at: datetime
    content: bytes
    content_hash: bytes
    record_metadata: dict


def generate_mock_records(
    count: int,
    *,
    record_types: list[str] | None = None,
    base_time: datetime | None = None,
) -> Iterator[MockRecord]:
    """Yield `count` records, round-robin across `record_types`.

    `occurred_at` walks backward from `base_time` in 1-second increments
    so the synthetic timeline is monotonically ordered and stable. The
    metering bucket (minute-aligned wall-clock at increment-call time)
    is unaffected — the test still lands every record in the same
    minute as far as the meter is concerned.
    """
    types = record_types or ["usage", "prompt"]
    t0 = base_time or datetime.now(timezone.utc).replace(microsecond=0)

    for i in range(count):
        rt = types[i % len(types)]
        content = f"mock-{rt}-{i:05d}".encode()
        yield MockRecord(
            record_type=rt,
            source_api="admin",
            external_id=f"ext-{uuid.uuid4().hex}",
            occurred_at=t0 - timedelta(seconds=count - i),
            content=content,
            content_hash=hashlib.sha256(content).digest(),
            record_metadata={
                "seq": i,
                "model": "claude-3-5-sonnet-20251022",
                "tokens": 100 + i % 500,
            },
        )
