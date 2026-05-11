# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Anthropic Admin API scheduled pull task (T3.5).

Two Celery tasks compose the steady-state pull pipeline:

  - `dispatch_admin_pulls` — beat-scheduled every 15 minutes. Opens a
    `scheduler_session_scope` (read-only, no `app.tenant_id` bound),
    enumerates active tenants in the current region, and fans out one
    `pull_admin_for_tenant.delay(tenant_id)` per row.
  - `pull_admin_for_tenant` — per-tenant. Loads the (tenant, "admin")
    cursor from `pull_state`, calls `admin_client_for_tenant` to get
    a client wired with the tenant's sealed admin key, iterates
    `client.list_usage(...)`, normalizes each `UsageBucket` to a
    telemetry_records row, and advances the cursor on success.

The actual work lives in `_pull_admin_for_tenant`, a pure-Python
function that accepts an optional `client` kwarg. Tests inject a
`MockTransport`-backed client; production calls the public Celery
wrapper which builds the client from sealed credentials.

Dedup: telemetry_records carries `UNIQUE (tenant_id, source_api,
external_id)`. A re-pull that hits an already-ingested bucket raises
`IntegrityError` from `append_telemetry_record`; we catch it and
count the dedup, leaving the existing chain row untouched. The
metering `increment` is gated by successful insert, so the count
matches the number of NEW records — not the number of iterations.

Cursor semantics: the cursor is the upper bound of what's been
successfully pulled. On first run (no cursor row), we default to a
1-day lookback to bootstrap the steady state. T3.6's backfill task
is the explicit "pull 90 days" entry point.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

from vargate_telemetry.anthropic import (
    AnthropicAdminClient,
    UsageBucket,
    admin_client_for_tenant,
)
from vargate_telemetry.celery_app import celery_app
from vargate_telemetry.chain import append_telemetry_record
from vargate_telemetry.db import engine, scheduler_session_scope, session_scope
from vargate_telemetry.metering import increment
from vargate_telemetry.metrics import observe_first_pull_if_first

_log = logging.getLogger(__name__)

# Source-API name used in pull_state + telemetry_records for this stream.
SOURCE_API_ADMIN = "admin"

# How far back to look on first run when no cursor exists.
DEFAULT_INITIAL_LOOKBACK_DAYS = 1

# Backfill defaults — T3.6 walks 90 days in 1-week chunks.
DEFAULT_BACKFILL_DAYS = 90
DEFAULT_BACKFILL_CHUNK_DAYS = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _maybe_observe_first_pull(tenant_id: str, inserted: int) -> None:
    """T4.7: emit the time-to-first-pull histogram observation if this
    pull was the first one to land a row for this tenant. Idempotent
    on a per-tenant basis via Redis SETNX inside
    `observe_first_pull_if_first`.

    Runs under the bootstrap role (no RLS GUC) because the lookup is
    on `users` (no RLS) and the per-tenant SELECT is by `tenant_id`.
    A best-effort signal: any error here is logged and swallowed so a
    metrics failure can never abort an otherwise-successful pull.
    """
    if inserted <= 0:
        return
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sql_text(
                    "SELECT sso_sign_in_at FROM users "
                    "WHERE tenant_id = :t AND sso_sign_in_at IS NOT NULL "
                    "ORDER BY sso_sign_in_at ASC LIMIT 1"
                ),
                {"t": tenant_id},
            ).first()
        if row is None or row.sso_sign_in_at is None:
            return
        observe_first_pull_if_first(tenant_id, row.sso_sign_in_at)
    except Exception:
        # Never let a metrics-side error abort or fail a successful
        # pull. Log loudly so we notice in monitoring.
        _log.exception(
            "first-pull metric observation failed for %s", tenant_id
        )


def _load_cursor(
    session, tenant_id: str, source_api: str
) -> Optional[datetime]:
    row = session.execute(
        sql_text(
            "SELECT cursor FROM pull_state "
            "WHERE tenant_id = :t AND source_api = :s"
        ),
        {"t": tenant_id, "s": source_api},
    ).first()
    if row is None or row.cursor is None:
        return None
    return datetime.fromisoformat(row.cursor)


def _save_cursor(
    session,
    tenant_id: str,
    source_api: str,
    cursor: datetime,
    status: str = "ok",
    error: Optional[str] = None,
) -> None:
    session.execute(
        sql_text(
            "INSERT INTO pull_state "
            "(tenant_id, source_api, cursor, last_pulled_at, "
            "last_status, last_error) "
            "VALUES (:t, :s, :c, :now, :status, :err) "
            "ON CONFLICT (tenant_id, source_api) "
            "DO UPDATE SET "
            "  cursor = EXCLUDED.cursor, "
            "  last_pulled_at = EXCLUDED.last_pulled_at, "
            "  last_status = EXCLUDED.last_status, "
            "  last_error = EXCLUDED.last_error"
        ),
        {
            "t": tenant_id,
            "s": source_api,
            "c": cursor.isoformat(),
            "now": _now(),
            "status": status,
            "err": error,
        },
    )


def _normalize_usage(bucket: UsageBucket) -> dict:
    """Turn one UsageBucket into telemetry_records insert kwargs.

    `external_id` is keyed off the bucket window so a re-pull of an
    already-ingested window dedups on insert via the UNIQUE constraint.
    `content_hash` is a SHA-256 over the canonical JSON of the whole
    bucket, so a refresh that changes nested `results` content surfaces
    as a hash mismatch even though the external_id matches.
    """
    bucket_dict = bucket.model_dump(mode="json")
    canonical = json.dumps(
        bucket_dict, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "record_type": "usage",
        "source_api": SOURCE_API_ADMIN,
        "external_id": (
            f"usage:{bucket.starting_at.isoformat()}:"
            f"{bucket.ending_at.isoformat()}"
        ),
        "occurred_at": bucket.starting_at,
        "content_hash": hashlib.sha256(canonical).digest(),
        "record_metadata": bucket_dict,
    }


def _pull_admin_for_tenant(
    tenant_id: str,
    *,
    client: Optional[AnthropicAdminClient] = None,
) -> dict[str, int]:
    """Pure-Python pull implementation. Returns {'inserted': N, 'deduped': M}."""
    if not tenant_id:
        raise ValueError("tenant_id required")

    # 1. Load cursor (own transaction, so the subsequent network call
    # doesn't hold the DB connection during HTTP I/O).
    with session_scope(tenant_id) as s:
        cursor = _load_cursor(s, tenant_id, SOURCE_API_ADMIN)

    pull_started = _now()
    starting_at = cursor or (
        pull_started - timedelta(days=DEFAULT_INITIAL_LOOKBACK_DAYS)
    )

    # 2. Build the Anthropic client unless one was injected.
    owned_client = client is None
    if owned_client:
        client = admin_client_for_tenant(tenant_id)

    inserted = 0
    deduped = 0
    try:
        for bucket in client.list_usage(
            starting_at=starting_at,
            ending_at=pull_started,
        ):
            fields = _normalize_usage(bucket)
            try:
                append_telemetry_record(tenant_id, **fields)
                increment(tenant_id, "usage")
                inserted += 1
            except IntegrityError:
                # Dedup hit on (tenant, source_api, external_id) UNIQUE.
                # Expected for re-pulls of an already-ingested window.
                deduped += 1
                _log.info(
                    "pull_admin: dedup hit for %s/%s",
                    tenant_id,
                    fields["external_id"],
                )

        # 3. Advance the cursor on success.
        with session_scope(tenant_id) as s:
            _save_cursor(
                s,
                tenant_id,
                SOURCE_API_ADMIN,
                pull_started,
                status="ok",
            )

        # 4. T4.7: emit time-to-first-pull histogram observation
        # if this pull was the first one to insert a row for the
        # tenant. Idempotent — only observes once per tenant.
        _maybe_observe_first_pull(tenant_id, inserted)
    finally:
        if owned_client:
            client.close()

    return {"inserted": inserted, "deduped": deduped}


@celery_app.task(
    bind=True,
    max_retries=3,
    name="vargate_telemetry.tasks.pull_admin.pull_admin_for_tenant",
)
def pull_admin_for_tenant(self, tenant_id: str) -> dict:
    """Beat-dispatched per-tenant pull. Retries on any exception."""
    try:
        return _pull_admin_for_tenant(tenant_id)
    except Exception as exc:
        _log.exception("pull_admin failed for %s", tenant_id)
        raise self.retry(exc=exc, countdown=60)


def _backfill_admin_for_tenant(
    tenant_id: str,
    days: int = DEFAULT_BACKFILL_DAYS,
    *,
    chunk_days: int = DEFAULT_BACKFILL_CHUNK_DAYS,
    client: Optional[AnthropicAdminClient] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> dict[str, int]:
    """Walk `days` of history in `chunk_days` slices, resumable on crash.

    Each chunk is one `client.list_usage(starting_at, ending_at)` call
    plus a `_save_cursor` after the chunk completes. A mid-backfill
    exception leaves the cursor pointing at the last successful chunk
    boundary, so the next invocation picks up from there — re-running
    `_backfill_admin_for_tenant` is the recovery path, no separate
    "resume" mode needed.

    If an existing cursor is later than `now - days`, the backfill
    starts from the cursor (resume) instead of `now - days`. This
    matters when (a) the steady-state pull task has already run and
    advanced the cursor past the requested backfill start, and (b)
    when an earlier backfill crashed mid-run.

    `progress_callback` is invoked at each chunk boundary with the
    cumulative `{chunks_processed, inserted, deduped}` dict. The
    Celery wrapper feeds this to `self.update_state(state='PROGRESS',
    meta=...)` so `/onboarding/backfill-status` can surface live
    counters to the frontend. Pure callers (the existing T3.6
    tests, the recovery path) pass None and the function stays
    side-effect-free with respect to Celery.

    Returns aggregated counts: inserted, deduped, chunks_processed.
    """
    if not tenant_id:
        raise ValueError("tenant_id required")
    if days <= 0:
        raise ValueError("days must be positive")
    if chunk_days <= 0:
        raise ValueError("chunk_days must be positive")

    with session_scope(tenant_id) as s:
        cursor = _load_cursor(s, tenant_id, SOURCE_API_ADMIN)

    now = _now()
    backfill_start = now - timedelta(days=days)
    start = max(cursor, backfill_start) if cursor else backfill_start

    owned_client = client is None
    if owned_client:
        client = admin_client_for_tenant(tenant_id)

    inserted = 0
    deduped = 0
    chunks_processed = 0

    try:
        chunk_start = start
        while chunk_start < now:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), now)

            for bucket in client.list_usage(
                starting_at=chunk_start,
                ending_at=chunk_end,
            ):
                fields = _normalize_usage(bucket)
                try:
                    append_telemetry_record(tenant_id, **fields)
                    increment(tenant_id, "usage")
                    inserted += 1
                except IntegrityError:
                    deduped += 1

            # Cursor advances chunk-by-chunk so a later crash resumes
            # cleanly from this point.
            with session_scope(tenant_id) as s:
                _save_cursor(
                    s,
                    tenant_id,
                    SOURCE_API_ADMIN,
                    chunk_end,
                    status="ok",
                )

            chunks_processed += 1
            chunk_start = chunk_end

            # T4.7: emit time-to-first-pull observation as soon as we
            # know the first chunk landed at least one row. The Redis
            # SETNX guard makes this safe to call on every chunk —
            # only the first call per tenant observes.
            _maybe_observe_first_pull(tenant_id, inserted)

            # Emit a PROGRESS tick AFTER the cursor advance, so a
            # status poll that races the chunk boundary sees the
            # already-committed state (not an in-flight chunk count
            # that a subsequent failure would unwind).
            if progress_callback is not None:
                try:
                    progress_callback(
                        {
                            "chunks_processed": chunks_processed,
                            "inserted": inserted,
                            "deduped": deduped,
                        }
                    )
                except Exception:  # pragma: no cover — never let the
                    # progress channel block the ingest path. A broken
                    # update_state shouldn't abort the backfill that's
                    # otherwise succeeding.
                    _log.exception(
                        "progress_callback raised for %s; ignoring",
                        tenant_id,
                    )
    finally:
        if owned_client:
            client.close()

    return {
        "inserted": inserted,
        "deduped": deduped,
        "chunks_processed": chunks_processed,
    }


@celery_app.task(
    bind=True,
    max_retries=3,
    name="vargate_telemetry.tasks.pull_admin.backfill_admin_for_tenant",
)
def backfill_admin_for_tenant(
    self,
    tenant_id: str,
    days: int = DEFAULT_BACKFILL_DAYS,
) -> dict:
    """One-shot Celery task: pull `days` of history for `tenant_id`.

    Threads `self.update_state(state='PROGRESS', meta=...)` into the
    pure helper so the T4.6 status endpoint can render live counters.
    """

    def _progress(meta: dict) -> None:
        self.update_state(state="PROGRESS", meta=meta)

    try:
        return _backfill_admin_for_tenant(
            tenant_id, days=days, progress_callback=_progress
        )
    except Exception as exc:
        _log.exception("backfill_admin failed for %s", tenant_id)
        raise self.retry(exc=exc, countdown=120)


@celery_app.task(
    name="vargate_telemetry.tasks.pull_admin.dispatch_admin_pulls",
)
def dispatch_admin_pulls(region: Optional[str] = None) -> int:
    """Beat fan-out. Enumerates active tenants and queues a pull per tenant.

    Runs under `scheduler_session_scope` so the cross-tenant SELECT on
    `tenants` is permitted (the role's GRANT posture is the gate, not
    RLS). Returns the count of dispatched tasks.
    """
    target_region = region or os.environ.get("VARGATE_REGION", "us")

    with scheduler_session_scope() as s:
        rows = s.execute(
            sql_text(
                "SELECT tenant_id FROM tenants "
                "WHERE active = true AND region = :r"
            ),
            {"r": target_region},
        ).all()

    for row in rows:
        pull_admin_for_tenant.delay(row.tenant_id)

    _log.info(
        "dispatch_admin_pulls: queued %d tenants in region %s",
        len(rows),
        target_region,
    )
    return len(rows)
