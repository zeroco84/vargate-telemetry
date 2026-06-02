# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Claude Code Analytics ingestion (T5.4).

Mirrors the T5.3 ``pull_compliance.py`` pattern: per-tenant cursor in
``pull_state``, ``_paginate``-via-the-client, ``append_telemetry_record``
into the audit chain, ``increment`` for metering, soft-skip on 403.
Differences specific to Code Analytics:

  - **Daily aggregation, not event stream.** The endpoint
    (``GET /v1/organizations/usage_report/claude_code``) takes a
    single ``starting_at`` date per call. The ingest loop walks
    forward day-by-day from the cursor, fetching all records for
    each day before advancing.
  - **Different pagination scheme** (within a single day). Code
    Analytics uses opaque ``page`` / ``next_page`` tokens — already
    handled by the client's existing ``paginate()`` helper; the
    ``list_code_analytics`` method just wraps that.
  - **Cursor encodes a DATE, not a TIMESTAMP.** Stored in
    ``pull_state.cursor`` as a UTC-midnight ISO datetime
    (compatible with the existing column shape).
  - **Plan-gating is loose.** Per the docs, the endpoint is free for
    all Admin-API-capable orgs — so the 403 soft-skip path is rare
    (Claude Platform on AWS is the documented exception). T5.4's
    real-org probe against the Personal-plan test org returned 200
    with empty data, confirming.

Activity-type-agnostic, just like the Activity Feed pull
=========================================================

Each Code Analytics record is one (actor, day) tuple with rich nested
metrics (core_metrics, tool_actions, model_breakdown). T5.4 stores the
full record JSON in ``telemetry_records.record_metadata`` and does NOT
branch on the shape — new tool types, new model names, new metric
keys ride along via ``extra="allow"`` on ``CodeAnalyticsRecord`` and
land in ``record_metadata`` unchanged. T5.5+ dashboards branch on
the nested structure.

Cursor semantics
================

  1. Load cursor from ``pull_state`` (or default to
     ``DEFAULT_INITIAL_LOOKBACK_DAYS`` if first run).
  2. Walk forward: ``next_day = max(cursor.date() + 1, today - LOOKBACK)``
     up to yesterday (today's data isn't complete per the 1-hour
     freshness lag).
  3. For each ``next_day``: ``client.list_code_analytics(starting_at=
     next_day)``, paginate via ``page`` tokens, persist each record.
  4. Advance cursor to ``next_day`` at end of day (commits each day's
     work atomically; a crash mid-day re-pulls the day with dedup).
  5. Bound by ``MAX_PAGES_PER_INVOCATION`` so a backfill-style large
     window doesn't monopolize a beat tick.

Dedup
=====

``telemetry_records`` carries ``UNIQUE (tenant_id, source_api,
external_id)``. The external_id we synthesize is
``code_analytics:{date}:{actor_email_or_api_key_name}``:

  - For ``user_actor``: ``code_analytics:2026-05-11:dev@example.com``
  - For ``api_actor``: ``code_analytics:2026-05-11:my-ci-key``

That's stable across re-pulls of the same day. The dedup path catches
re-ingestion cleanly and the cursor still advances (matches T5.3's
"advance even on dedup-only runs" pattern, since dedup'd days are
already-have-everything days from our perspective).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

from vargate_telemetry.anthropic import (
    AnthropicAdminClient,
    CodeAnalyticsRecord,
    InsufficientScope,
    admin_client_for_tenant,
)
from vargate_telemetry.celery_app import celery_app
from vargate_telemetry.chain import append_telemetry_record
from vargate_telemetry.db import scheduler_session_scope, session_scope
from vargate_telemetry.metering import increment


_log = logging.getLogger(__name__)


# Source-API name used in pull_state + telemetry_records for this stream.
# Distinct from the other source_api values so the cursors don't collide
# and the dashboard can filter by stream.
SOURCE_API_CODE_ANALYTICS = "code_analytics"

# Daily-aggregated data has a 1-hour freshness lag per the docs. We only
# ingest days whose UTC midnight is at least 1 day in the past, so
# the day's data is settled before we ask for it.
INGEST_LAG_DAYS = 1

# How far back to look on first run when no cursor exists. 7 days
# gives a useful initial dataset for a new tenant without an excessive
# backfill — admins typically want a week of trend.
DEFAULT_INITIAL_LOOKBACK_DAYS = 7

# Page size. Anthropic's default is 20, max 1000. 100 balances request
# count against per-page payload; a busy org with 100+ Code Code users
# in one day will trigger pagination within the day's pull.
DEFAULT_PER_PAGE_LIMIT = 100

# Cap days per invocation. At up to ~50 pages of 100 records each per
# day, this is plenty for any reasonable org. Subsequent ticks pick up
# from the cursor on the next 15-minute beat.
MAX_PAGES_PER_INVOCATION = 50


# ───────────────────────────────────────────────────────────────────────────
# Helpers — cursor I/O + record normalization
# ───────────────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_today() -> date:
    return _now().date()


def _midnight(d: date) -> datetime:
    """Return the UTC midnight datetime for ``d`` — the cursor format."""
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)


def _load_cursor(
    session, tenant_id: str, source_api: str
) -> Optional[datetime]:
    """Mirror of ``pull_compliance._load_cursor`` — kept here as a
    sibling rather than refactored into a shared helper because each
    stream's cursor semantics differ slightly (Activity Feed: max
    created_at; Code Analytics: last successfully ingested DAY)."""
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
    *,
    status: str = "ok",
    error: Optional[str] = None,
) -> None:
    """UPSERT the cursor for (tenant, source_api). Mirrors pull_admin's
    and pull_compliance's helpers."""
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


def _actor_handle(record: CodeAnalyticsRecord) -> str:
    """Stable per-(actor) string for the external_id.

    For ``user_actor``: email_address. For ``api_actor``:
    api_key_name (note: Code Analytics uses *name*, NOT id). Falls
    back to ``actor.type:unknown`` if neither is set (shouldn't happen
    in practice — the docs guarantee one of the two — but a defensive
    fallback keeps external_id non-empty if Anthropic ships a novel
    actor variant).
    """
    actor = record.actor
    if actor.type == "user_actor" and actor.email_address:
        return actor.email_address
    if actor.type == "api_actor" and actor.api_key_name:
        return actor.api_key_name
    # Defensive: keep external_id non-empty + stable per record so
    # dedup still works for unknown actor variants.
    return f"{actor.type}:unknown"


def _normalize_record(record: CodeAnalyticsRecord) -> dict[str, Any]:
    """Turn one CodeAnalyticsRecord into telemetry_records insert kwargs.

    ``external_id`` keys off (date, actor) so re-pulls of the same day
    dedup on the UNIQUE constraint.

    ``content_hash`` is SHA-256 over the canonical JSON of the full
    record (same pattern as the Activity Feed + Admin pulls). Catches
    any post-hoc value change as a chain hash mismatch.

    No branching on the nested ``core_metrics`` / ``tool_actions`` /
    ``model_breakdown`` shapes — the full record JSON lands verbatim
    in ``record_metadata``.
    """
    record_dict = record.model_dump(mode="json")
    canonical = json.dumps(
        record_dict, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    day_iso = record.date.date().isoformat()
    return {
        "record_type": "code_analytics",
        "source_api": SOURCE_API_CODE_ANALYTICS,
        "external_id": (
            f"code_analytics:{day_iso}:{_actor_handle(record)}"
        ),
        "occurred_at": record.date,
        "content_hash": hashlib.sha256(canonical).digest(),
        "record_metadata": record_dict,
    }


# ───────────────────────────────────────────────────────────────────────────
# Pure-Python implementation — testable without a Celery worker
# ───────────────────────────────────────────────────────────────────────────


def _pull_code_analytics_for_tenant(
    tenant_id: str,
    *,
    since: Optional[date] = None,
    client: Optional[AnthropicAdminClient] = None,
    per_page_limit: int = DEFAULT_PER_PAGE_LIMIT,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Pull Code Analytics records day-by-day since the cursor; persist
    + advance.

    Returns ``{"records_pulled": N, "records_deduped": M,
    "days_processed": D, "status": "ok"}`` on success.

    On 403 (rare for this endpoint — see module docstring), returns
    ``{"records_pulled": 0, "records_deduped": 0, "days_processed": 0,
    "status": "no_code_analytics_access"}`` rather than raising. The
    dispatcher should NOT retry on this condition.

    ``since`` and ``today`` overrides are for tests and for future
    "force re-ingest from date X" tooling. Production callers leave
    them at their defaults (load from cursor; use real wall-clock).
    """
    if not tenant_id:
        raise ValueError("tenant_id required")

    today_utc = today or _utc_today()
    # Data has a freshness lag — only ingest days <= today - INGEST_LAG_DAYS
    # (i.e. yesterday in normal operation). Today's data isn't settled.
    last_complete_day = today_utc - timedelta(days=INGEST_LAG_DAYS)

    # 1. Load cursor (own transaction; HTTP I/O follows).
    if since is not None:
        next_day = since
    else:
        with session_scope(tenant_id) as s:
            cursor = _load_cursor(s, tenant_id, SOURCE_API_CODE_ANALYTICS)
        if cursor is not None:
            next_day = cursor.date() + timedelta(days=1)
        else:
            next_day = today_utc - timedelta(
                days=DEFAULT_INITIAL_LOOKBACK_DAYS
            )

    # 2. Build the Anthropic client unless one was injected.
    owned_client = client is None
    if owned_client:
        client = admin_client_for_tenant(tenant_id)

    records_pulled = 0
    records_deduped = 0
    days_processed = 0
    pages_done = 0

    try:
        while next_day <= last_complete_day:
            if pages_done >= MAX_PAGES_PER_INVOCATION:
                _log.warning(
                    "pull_code_analytics: hit per-invocation page cap "
                    "(%d) for %s at day=%s; cursor advances to %s, "
                    "rest resumes next tick",
                    MAX_PAGES_PER_INVOCATION,
                    tenant_id,
                    next_day,
                    next_day - timedelta(days=1),
                )
                break

            try:
                for record in client.list_code_analytics(
                    starting_at=next_day, limit=per_page_limit
                ):
                    fields = _normalize_record(record)
                    try:
                        append_telemetry_record(tenant_id, **fields)
                        increment(tenant_id, "code_analytics")
                        records_pulled += 1
                    except IntegrityError:
                        records_deduped += 1
                        _log.info(
                            "pull_code_analytics: dedup hit %s/%s",
                            tenant_id,
                            fields["external_id"],
                        )
                    # Crude pages-counter: each `per_page_limit` records
                    # processed marks a page boundary.
                    if (
                        (records_pulled + records_deduped)
                        % per_page_limit
                        == 0
                    ):
                        pages_done += 1
                        if pages_done >= MAX_PAGES_PER_INVOCATION:
                            break
            except InsufficientScope:
                _log.info(
                    "pull_code_analytics: 403 no_code_analytics_access "
                    "for %s",
                    tenant_id,
                )
                return {
                    "records_pulled": 0,
                    "records_deduped": 0,
                    "days_processed": 0,
                    "status": "no_code_analytics_access",
                }

            # End-of-day: advance cursor (status='ok'). Each day's work
            # is committed independently — a crash before the next day
            # leaves us at this day's midnight, and the resume picks up
            # with `next_day + 1`.
            with session_scope(tenant_id) as s:
                _save_cursor(
                    s,
                    tenant_id,
                    SOURCE_API_CODE_ANALYTICS,
                    _midnight(next_day),
                    status="ok",
                )
            days_processed += 1
            next_day += timedelta(days=1)
    finally:
        if owned_client:
            client.close()

    return {
        "records_pulled": records_pulled,
        "records_deduped": records_deduped,
        "days_processed": days_processed,
        "status": "ok",
    }


# ───────────────────────────────────────────────────────────────────────────
# Celery task wrappers
# ───────────────────────────────────────────────────────────────────────────


@celery_app.task(
    bind=True,
    max_retries=3,
    name="vargate_telemetry.tasks.pull_code_analytics.pull_code_analytics_for_tenant",
)
def pull_code_analytics_for_tenant(self, tenant_id: str) -> dict[str, Any]:
    """Beat-dispatched per-tenant Code Analytics pull. Retries on any
    exception OTHER than the 403 soft-skip (which returns cleanly)."""
    try:
        return _pull_code_analytics_for_tenant(tenant_id)
    except Exception as exc:
        _log.exception("pull_code_analytics failed for %s", tenant_id)
        raise self.retry(exc=exc, countdown=120)


# ───────────────────────────────────────────────────────────────────────────
# Dispatcher (beat fan-out)
# ───────────────────────────────────────────────────────────────────────────


@celery_app.task(
    name="vargate_telemetry.tasks.pull_code_analytics.dispatch_code_analytics_pulls",
)
def dispatch_code_analytics_pulls(region: Optional[str] = None) -> int:
    """Beat fan-out for the Code Analytics stream.

    Enumerates active tenants (all regions by default; pass ``region``
    to restrict) and queues one ``pull_code_analytics_for_tenant`` per
    row. Returns the count.

    Mirrors ``pull_admin.dispatch_admin_pulls`` and
    ``pull_compliance.dispatch_compliance_activity_pulls`` — same
    scheduler-role session scope, same per-tenant cursor model,
    separate task name + cursor row so the streams advance
    independently.

    Doesn't filter on a persisted ``capabilities.code_analytics``
    flag (per the spec discussion: that field isn't persisted per
    tenant today). The per-tenant pull soft-skips on 403, which is
    the documented "no Code Analytics access" path. Since Code
    Analytics is free for all Admin-API orgs per Anthropic's docs,
    that path is rare in practice.
    """
    # TM5 T5.0: default dispatches all active tenants; the region gap
    # (defaulting to VARGATE_REGION=us) silently skipped eu tenants.
    # region arg kept as an explicit override.
    with scheduler_session_scope() as s:
        if region is None:
            rows = s.execute(
                sql_text(
                    "SELECT tenant_id FROM tenants WHERE active = true"
                ),
            ).all()
        else:
            rows = s.execute(
                sql_text(
                    "SELECT tenant_id FROM tenants "
                    "WHERE active = true AND region = :r"
                ),
                {"r": region},
            ).all()

    for row in rows:
        pull_code_analytics_for_tenant.delay(row.tenant_id)

    _log.info(
        "dispatch_code_analytics_pulls: queued %d tenants in region %s",
        len(rows),
        region or "all",
    )
    return len(rows)


__all__ = [
    "DEFAULT_INITIAL_LOOKBACK_DAYS",
    "DEFAULT_PER_PAGE_LIMIT",
    "INGEST_LAG_DAYS",
    "MAX_PAGES_PER_INVOCATION",
    "SOURCE_API_CODE_ANALYTICS",
    "_pull_code_analytics_for_tenant",
    "dispatch_code_analytics_pulls",
    "pull_code_analytics_for_tenant",
]
