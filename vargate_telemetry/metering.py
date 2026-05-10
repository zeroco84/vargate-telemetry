# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Per-tenant ingest metering — Redis counters + Postgres flush (T2.3).

Every successful ingest into `telemetry_records` calls
`increment(tenant_id, record_type)`. The counter lives in a single
Redis hash, sharded by field on `(tenant_id, record_type, minute_bucket)`.
A Celery beat task (`vargate_telemetry.tasks.metering`) calls `flush()`
every 60 seconds; flush atomically renames the active hash to a
per-task flush hash, drains it into `usage_records` with ON CONFLICT
DO UPDATE, then deletes the flush hash.

Concurrency:
  - Two flush tasks running simultaneously each get a distinct flush
    key (uuid suffix). Redis serializes the RENAME, so only one task
    captures any given active-hash state; the loser sees no active
    hash and returns 0.
  - The active hash carries a 2-hour TTL as a safety net — if no
    flush ever runs, increments older than 2h are dropped rather
    than accumulating unboundedly.

Atomicity:
  - Per-tenant batches are wrapped in a single `session_scope`
    transaction. If any UPSERT raises, the whole batch rolls back —
    no partial usage_records rows for that tenant.
  - The flush hash is deleted only after every per-tenant batch
    commits. On a mid-flush crash, the flush hash survives (with
    TTL) and a janitor / next-tick retry can pick it up.

Field encoding: Redis hash fields are
`<tenant_id>\\x1f<record_type>\\x1f<bucket_iso>`. ASCII Unit Separator
(0x1f) was chosen because it cannot appear in real tenant_id or
record_type values; we still validate at the `increment` boundary.
"""

from __future__ import annotations

import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import redis
from sqlalchemy import text

from vargate_telemetry.db import session_scope

# Redis key namespace.
_ACTIVE_HASH = "vargate:meter:active"
_FLUSH_PREFIX = "vargate:meter:flush:"
_TTL_SECONDS = 7200  # 2-hour safety net

# Field delimiter inside Redis hash fields. ASCII Unit Separator (0x1f).
_FIELD_DELIM = "\x1f"

_redis_client: Optional[redis.Redis] = None


def _redis() -> redis.Redis:
    """Lazy-initialize the Redis client from REDIS_URL.

    Module-level lazy init so importing this module doesn't require
    REDIS_URL to be set (which matters for test discovery in
    environments where Redis isn't reachable).
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(
            os.environ["REDIS_URL"], decode_responses=False
        )
    return _redis_client


def _reset_for_test() -> None:
    """Drop the cached Redis client. Test-only escape hatch."""
    global _redis_client
    _redis_client = None


def _minute_bucket() -> datetime:
    """Current minute-aligned UTC timestamp."""
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


def increment(tenant_id: str, record_type: str, n: int = 1) -> None:
    """Increment the (tenant, record_type, current_minute) counter by `n`.

    Cheap: one Redis pipeline of HINCRBY + EXPIRE, no Postgres I/O.
    Hot-path callers can fire-and-forget; the flush task drains every
    60 seconds.
    """
    if not tenant_id or not record_type:
        raise ValueError("tenant_id and record_type required")
    if _FIELD_DELIM in tenant_id or _FIELD_DELIM in record_type:
        raise ValueError(
            "tenant_id and record_type must not contain U+001F (Unit Separator)"
        )
    if n <= 0:
        raise ValueError("n must be positive")

    bucket_iso = _minute_bucket().isoformat()
    field = f"{tenant_id}{_FIELD_DELIM}{record_type}{_FIELD_DELIM}{bucket_iso}"

    r = _redis()
    pipe = r.pipeline()
    pipe.hincrby(_ACTIVE_HASH, field, n)
    pipe.expire(_ACTIVE_HASH, _TTL_SECONDS)
    pipe.execute()


def flush() -> int:
    """Drain the active counter hash into `usage_records`. Returns rows processed.

    Concurrency-safe: each call generates a unique flush key, so two
    parallel flushes can't see the same data. If the active hash is
    empty / missing (no recent increments), this is a no-op.
    """
    r = _redis()
    flush_key = f"{_FLUSH_PREFIX}{uuid.uuid4().hex}"

    try:
        r.rename(_ACTIVE_HASH, flush_key)
    except redis.ResponseError:
        # Active hash doesn't exist — nothing to flush.
        return 0

    # TTL on the flush key so a mid-flush crash doesn't leak the
    # buffer forever; ops can also see it during incident response.
    r.expire(flush_key, _TTL_SECONDS)

    entries = r.hgetall(flush_key)
    if not entries:
        r.delete(flush_key)
        return 0

    # Group by tenant — RLS forces one session_scope per tenant.
    by_tenant: dict[str, list[tuple[str, datetime, int]]] = defaultdict(list)
    for field_bytes, count_bytes in entries.items():
        field = field_bytes.decode("utf-8")
        tenant_id, record_type, bucket_iso = field.split(_FIELD_DELIM, 2)
        bucket_start = datetime.fromisoformat(bucket_iso)
        by_tenant[tenant_id].append(
            (record_type, bucket_start, int(count_bytes))
        )

    processed = 0
    for tenant_id, items in by_tenant.items():
        _upsert_usage(tenant_id, items)
        processed += len(items)

    # All per-tenant batches committed — safe to drop the flush hash.
    r.delete(flush_key)
    return processed


def _upsert_usage(
    tenant_id: str,
    items: list[tuple[str, datetime, int]],
) -> None:
    """UPSERT one tenant's batch of counters into `usage_records`.

    Single `session_scope` transaction. ON CONFLICT DO UPDATE folds
    new increments into any existing row for the same
    (tenant_id, bucket_start, record_type), so repeated flushes that
    span the same minute bucket never double-count.

    After the UPSERT, dispatch Stripe usage events for the same items
    under the same transaction (T2.4). Dispatch failures land in
    `billing_retry` instead of raising — Stripe outages must not block
    metering durability. The idempotency key is derived from
    (tenant, record_type, bucket_start), so a flush replayed after a
    worker crash never double-charges.
    """
    if not items:
        return

    # Lazy import to keep `metering` importable in environments that
    # haven't installed `stripe` yet (e.g. test discovery on a clean box).
    from vargate_telemetry.billing import report_usage

    with session_scope(tenant_id) as s:
        for record_type, bucket_start, count in items:
            s.execute(
                text(
                    "INSERT INTO usage_records "
                    "(tenant_id, bucket_start, record_type, record_count) "
                    "VALUES (:tenant_id, :bucket_start, :record_type, :record_count) "
                    "ON CONFLICT (tenant_id, bucket_start, record_type) "
                    "DO UPDATE SET "
                    "  record_count = usage_records.record_count + EXCLUDED.record_count"
                ),
                {
                    "tenant_id": tenant_id,
                    "bucket_start": bucket_start,
                    "record_type": record_type,
                    "record_count": count,
                },
            )
        report_usage(s, tenant_id, items)
