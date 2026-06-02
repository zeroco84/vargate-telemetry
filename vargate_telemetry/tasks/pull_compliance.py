# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Compliance API ingestion (T5.3).

Two ingestion streams live here, mirroring the Compliance API's
two-endpoint-family split:

  - **Activity Feed** (``GET /v1/compliance/activities``). Reachable by
    Admin API keys carrying ``read:compliance_activities`` scope.
    Returns event METADATA — chat-created, file-uploaded, sign-in,
    admin events — but NOT prompt/response text. T5.3 ships this
    pipeline. ``pull_activities_for_tenant`` is the per-tenant worker;
    ``dispatch_compliance_activity_pulls`` is the 15-minute beat
    fan-out.
  - **Content endpoints** (``/v1/compliance/apps/chats/*``). Require a
    separate **Compliance Access Key** (``sk-ant-api01-...``) created
    in claude.ai by an Enterprise Primary Owner. T5.3 ships the
    ``pull_content_for_tenant`` STUB — the function structure is in
    place so the dispatcher can call it per-tenant, but the body
    raises ``NotConfigured`` because no tenant has a sealed
    Compliance Access Key yet. A future sprint adds the onboarding
    flow that collects it and replaces the stub body.

Why a stub instead of just deleting the call site
==================================================

The dispatcher pattern below already iterates active tenants and
fan-outs ``pull_content_for_tenant`` per row. When the Compliance
Access Key flow lands, it's a single function-body fill-in here, not
a dispatcher refactor. Documented inline so future-you doesn't think
the stub is a forgotten TODO.

Activity-type-agnostic ingest
=============================

The Activity Feed returns hundreds of distinct ``type`` values
(``claude_chat_created``, ``claude_file_uploaded``,
``sso_login_initiated``, ``admin_api_key_created``, etc.). We do
NOT branch on type in ingest — every activity lands as a
``telemetry_record`` with ``record_type='activity'``,
``source_api='compliance_activities'``, and the full activity JSON
in ``record_metadata``. The dashboard surfaces type-specific views
in later sprints; keeping ingest type-agnostic means new Anthropic
activity types absorb cleanly without an ingest-side code change.

Cursor semantics
================

The activity cursor is the latest ``created_at`` we've successfully
ingested. On each invocation:

  1. Load cursor from ``pull_state`` (or default to
     ``DEFAULT_INITIAL_LOOKBACK_DAYS`` if first run).
  2. Call ``list_activities(created_at_gte=cursor, limit=...)``.
  3. For each activity (newest-first), insert via
     ``append_telemetry_record`` (chain-bound) and track the max
     ``created_at`` seen.
  4. Advance the cursor to the max(created_at) of this run, or to
     the wall-clock now if no activities were returned (so we don't
     re-query an empty window on the next tick).

Dedup
=====

``telemetry_records`` carries ``UNIQUE (tenant_id, source_api,
external_id)``. The external_id we use is ``activity.id`` — a stable
Anthropic-assigned identifier like
``activity_01XyDMpzjS89pFZXqSFUBDr6``. Re-ingesting the same window
hits the UNIQUE constraint and ``append_telemetry_record`` raises
``IntegrityError``; we count the dedup and proceed. Metering's
``increment`` is gated by successful insert, so the count matches
NEW rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

from vargate_telemetry.anthropic import (
    Activity,
    AnthropicAdminClient,
    InsufficientScope,
    admin_client_for_tenant,
)
from vargate_telemetry.celery_app import celery_app
from vargate_telemetry.chain import append_telemetry_record
from vargate_telemetry.db import scheduler_session_scope, session_scope
from vargate_telemetry.metering import increment


_log = logging.getLogger(__name__)


# Source-API name used in pull_state + telemetry_records for this stream.
# Distinct from `SOURCE_API_ADMIN` so the cursors don't collide and the
# dashboard can filter by stream.
SOURCE_API_ACTIVITIES = "compliance_activities"

# Source-API name reserved for the content stream (T5.x). Declared here so
# the dispatcher can construct the cursor key without importing across
# stub boundaries.
SOURCE_API_CONTENT = "compliance_content"

# How far back to look on first run when no cursor exists. Activities
# accumulate fast in an active org; a 1-day lookback gives the first
# ingest a useful initial dataset without flooding.
DEFAULT_INITIAL_LOOKBACK_DAYS = 1

# Cap pages per invocation to bound per-tick work — at default limit=100
# and 50 pages cap that's 5000 activities per tenant per tick. Anything
# beyond rolls forward on the next 15-minute tick.
DEFAULT_PER_PAGE_LIMIT = 100
MAX_PAGES_PER_INVOCATION = 50


# ───────────────────────────────────────────────────────────────────────────
# Public exception types
# ───────────────────────────────────────────────────────────────────────────


class NotConfigured(Exception):
    """The tenant has no sealed credential for this ingest stream.

    Raised by ``pull_content_for_tenant`` when the tenant has no
    Compliance Access Key sealed in ``encrypted_secrets``. The
    dispatcher catches this and logs+skips; it is NOT a retryable
    error.
    """


# ───────────────────────────────────────────────────────────────────────────
# Helpers — cursor I/O + record normalization
# ───────────────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_cursor(
    session, tenant_id: str, source_api: str
) -> Optional[datetime]:
    """Read the cursor for (tenant, source_api). None if no row yet.

    Mirrors ``pull_admin._load_cursor`` — kept here as a sibling
    rather than refactored into a shared helper because the cursor
    semantics are stream-specific (activities use max-created_at,
    admin pulls use end-of-window).
    """
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
    """UPSERT the cursor for (tenant, source_api). Mirrors pull_admin's."""
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


def _normalize_activity(activity: Activity) -> dict[str, Any]:
    """Turn one Activity into telemetry_records insert kwargs.

    ``external_id`` is the Anthropic-assigned activity ID — stable
    across re-ingest and the natural dedup key.

    ``content_hash`` is SHA-256 over the canonical JSON of the full
    activity dict (same pattern as ``pull_admin._normalize_usage``).
    For records with no content blob this is a fingerprint of the
    metadata; if Anthropic ever changes a field on an already-ingested
    activity, the chain catches it as a hash mismatch on the next
    re-pull attempt.

    No branching on ``activity.type`` — the full activity JSON
    (including type-specific extra fields like ``claude_chat_id`` or
    ``filename``) lands verbatim in ``record_metadata``. The dashboard
    surfaces per-type views in later sprints.
    """
    activity_dict = activity.model_dump(mode="json")
    canonical = json.dumps(
        activity_dict, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "record_type": "activity",
        "source_api": SOURCE_API_ACTIVITIES,
        "external_id": activity.id,
        "occurred_at": activity.created_at,
        "content_hash": hashlib.sha256(canonical).digest(),
        "record_metadata": activity_dict,
    }


# ───────────────────────────────────────────────────────────────────────────
# Pure-Python implementation — testable without a Celery worker
# ───────────────────────────────────────────────────────────────────────────


def _pull_activities_for_tenant(
    tenant_id: str,
    *,
    since: Optional[datetime] = None,
    client: Optional[AnthropicAdminClient] = None,
    per_page_limit: int = DEFAULT_PER_PAGE_LIMIT,
) -> dict[str, Any]:
    """Pull activity-feed records since the cursor; persist + advance.

    Returns ``{"activities_pulled": N, "activities_deduped": M,
    "status": "ok"}`` on success.

    On 403 (key lacks ``read:compliance_activities`` scope or the
    org's plan tier doesn't include Compliance API), returns
    ``{"activities_pulled": 0, "activities_deduped": 0,
    "status": "no_activity_feed_access"}`` rather than raising — the
    dispatcher should NOT retry on this condition, it should skip
    the tenant for this stream.

    ``since`` overrides the loaded cursor if supplied (used by tests
    and by future "force re-ingest from date X" tooling).
    """
    if not tenant_id:
        raise ValueError("tenant_id required")

    # 1. Load cursor (own transaction; HTTP I/O follows).
    if since is not None:
        starting_at = since
    else:
        with session_scope(tenant_id) as s:
            cursor = _load_cursor(s, tenant_id, SOURCE_API_ACTIVITIES)
        starting_at = cursor or (
            _now() - timedelta(days=DEFAULT_INITIAL_LOOKBACK_DAYS)
        )

    pull_started = _now()

    # 2. Build the Anthropic client unless one was injected.
    owned_client = client is None
    if owned_client:
        client = admin_client_for_tenant(tenant_id)

    activities_pulled = 0
    activities_deduped = 0
    max_created_at_seen = starting_at

    try:
        try:
            # Pull all activities since cursor. The compliance pagination
            # walks newest-first; the running max(created_at) is the new
            # cursor on success.
            page_count = 0
            for activity in client.list_activities(
                created_at_gte=starting_at,
                limit=per_page_limit,
            ):
                fields = _normalize_activity(activity)
                try:
                    append_telemetry_record(tenant_id, **fields)
                    increment(tenant_id, "activity")
                    activities_pulled += 1
                except IntegrityError:
                    # UNIQUE (tenant_id, source_api, external_id) hit.
                    # Expected on re-runs that overlap the cursor window.
                    activities_deduped += 1
                    _log.info(
                        "pull_compliance: dedup hit %s/%s",
                        tenant_id,
                        fields["external_id"],
                    )
                if activity.created_at > max_created_at_seen:
                    max_created_at_seen = activity.created_at

                # Bound per-invocation work. Subsequent ticks pick up
                # where we left off via the persisted cursor.
                # `list_activities` yields across pages internally; we
                # approximate page count by row count / page limit.
                page_count = (
                    (activities_pulled + activities_deduped)
                    // per_page_limit
                )
                if page_count >= MAX_PAGES_PER_INVOCATION:
                    _log.warning(
                        "pull_compliance: hit per-invocation page cap "
                        "(%d) for %s; advancing cursor and yielding to "
                        "next tick",
                        MAX_PAGES_PER_INVOCATION,
                        tenant_id,
                    )
                    break
        except InsufficientScope:
            # Tenant's admin key lacks `read:compliance_activities` or
            # the org's plan doesn't include Compliance API. Don't
            # retry — surface as a soft skip so the dispatcher can
            # log and move on.
            _log.info(
                "pull_compliance: 403 no_activity_feed_access for %s",
                tenant_id,
            )
            return {
                "activities_pulled": 0,
                "activities_deduped": 0,
                "status": "no_activity_feed_access",
            }

        # 3. Advance the cursor on success. If we ingested at least
        # one activity, use its created_at; otherwise use pull_started
        # so we don't re-query an empty window forever.
        new_cursor = (
            max_created_at_seen
            if activities_pulled or activities_deduped
            else pull_started
        )
        with session_scope(tenant_id) as s:
            _save_cursor(
                s,
                tenant_id,
                SOURCE_API_ACTIVITIES,
                new_cursor,
                status="ok",
            )
    finally:
        if owned_client:
            client.close()

    return {
        "activities_pulled": activities_pulled,
        "activities_deduped": activities_deduped,
        "status": "ok",
    }


# ───────────────────────────────────────────────────────────────────────────
# Content-stream stub — body lands in a future sprint
# ───────────────────────────────────────────────────────────────────────────


def _pull_content_for_tenant(tenant_id: str) -> dict[str, Any]:
    """Stub for the content ingestion stream (T5.x).

    Raises ``NotConfigured`` because today's onboarding doesn't
    collect a Compliance Access Key. The dispatcher pattern below
    already calls this per-tenant; when the Compliance Access Key
    onboarding flow lands, this stub's body is replaced with the
    real ``client.list_chats`` → ``get_chat_messages`` →
    ``store_content`` → ``append_telemetry_record`` pipeline.

    Why a stub, not just an omission: keeping the function shape
    here means the dispatcher's iteration loop doesn't need to grow
    a conditional branch for "is content stream wired yet?" — it
    just catches ``NotConfigured`` and skips. The control flow at
    activation time is "remove the raise, fill in the body" rather
    than "thread a new task name through the dispatcher."
    """
    if not tenant_id:
        raise ValueError("tenant_id required")
    raise NotConfigured(
        f"Compliance Access Key not configured for tenant {tenant_id!r}. "
        "Content ingestion requires a separate sk-ant-api01-* key "
        "provisioned via the (future) compliance-access-key onboarding "
        "flow. Activity Feed ingest is unaffected."
    )


# ───────────────────────────────────────────────────────────────────────────
# Celery task wrappers
# ───────────────────────────────────────────────────────────────────────────


@celery_app.task(
    bind=True,
    max_retries=3,
    name="vargate_telemetry.tasks.pull_compliance.pull_activities_for_tenant",
)
def pull_activities_for_tenant(self, tenant_id: str) -> dict[str, Any]:
    """Beat-dispatched per-tenant Activity Feed pull. Retries on any
    exception OTHER than the 403 soft-skip (which returns cleanly)."""
    try:
        return _pull_activities_for_tenant(tenant_id)
    except Exception as exc:
        _log.exception("pull_activities failed for %s", tenant_id)
        raise self.retry(exc=exc, countdown=120)


@celery_app.task(
    bind=True,
    max_retries=0,
    name="vargate_telemetry.tasks.pull_compliance.pull_content_for_tenant",
)
def pull_content_for_tenant(self, tenant_id: str) -> dict[str, Any]:
    """Beat-dispatched per-tenant Content pull (T5.x).

    Today: always raises ``NotConfigured``. The dispatcher catches
    this and skips. Wrapped here as a Celery task so the dispatcher
    fan-out signature is identical to other streams.
    """
    return _pull_content_for_tenant(tenant_id)


# ───────────────────────────────────────────────────────────────────────────
# Dispatchers (beat fan-out)
# ───────────────────────────────────────────────────────────────────────────


@celery_app.task(
    name="vargate_telemetry.tasks.pull_compliance.dispatch_compliance_activity_pulls",
)
def dispatch_compliance_activity_pulls(region: Optional[str] = None) -> int:
    """Beat fan-out for the Activity Feed stream.

    Enumerates active tenants in the current region and queues one
    ``pull_activities_for_tenant`` per row. Returns the count.

    Mirrors ``pull_admin.dispatch_admin_pulls`` — same scheduler-role
    session scope, same per-tenant cursor model, separate task name +
    queue so the two streams don't share a beat slot.
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
        pull_activities_for_tenant.delay(row.tenant_id)

    _log.info(
        "dispatch_compliance_activity_pulls: queued %d tenants in region %s",
        len(rows),
        region or "all",
    )
    return len(rows)


@celery_app.task(
    name="vargate_telemetry.tasks.pull_compliance.dispatch_compliance_content_pulls",
)
def dispatch_compliance_content_pulls(region: Optional[str] = None) -> int:
    """Beat fan-out for the Content stream (T5.x).

    Today: every dispatched task immediately raises ``NotConfigured``
    because no tenant has a sealed Compliance Access Key yet. The
    dispatcher itself catches the raise inside the per-tenant task
    and logs+skips — see ``pull_content_for_tenant``. We still queue
    the tasks so the metrics show "N tenants attempted, N skipped"
    rather than silently producing zero activity.

    Future sprint: when the Compliance Access Key onboarding flow
    lands, no change required here — the per-tenant task's body
    starts succeeding and counts flow through.
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

    skipped = 0
    for row in rows:
        # Call the pure-Python stub directly inside the dispatcher
        # (rather than `.delay`-ing) so the NotConfigured raises here
        # and we can count skips without polluting Celery's failure
        # metrics with expected errors. When T5.x activates content
        # ingestion, switch this to `.delay(row.tenant_id)`.
        try:
            _pull_content_for_tenant(row.tenant_id)
        except NotConfigured as exc:
            skipped += 1
            _log.info(
                "pull_content skipped for %s: %s",
                row.tenant_id,
                exc,
            )

    _log.info(
        "dispatch_compliance_content_pulls: %d tenants, %d skipped "
        "(no Compliance Access Key configured), region %s",
        len(rows),
        skipped,
        region or "all",
    )
    return len(rows)


__all__ = [
    "DEFAULT_INITIAL_LOOKBACK_DAYS",
    "DEFAULT_PER_PAGE_LIMIT",
    "MAX_PAGES_PER_INVOCATION",
    "NotConfigured",
    "SOURCE_API_ACTIVITIES",
    "SOURCE_API_CONTENT",
    "_pull_activities_for_tenant",
    "_pull_content_for_tenant",
    "dispatch_compliance_activity_pulls",
    "dispatch_compliance_content_pulls",
    "pull_activities_for_tenant",
    "pull_content_for_tenant",
]
