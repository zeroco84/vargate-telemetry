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
    in claude.ai by an Enterprise owner (collected via the TM5 T5.1
    onboarding flow). ``pull_content_for_tenant`` (T5.2) walks orgs →
    users → chats → messages and stores each message's TEXT encrypted
    under the tenant DEK (MinIO via ``store_content``) with a
    chain-bound ``telemetry_record``. A tenant with no sealed key
    soft-skips (``status="no_content_key"``); the pull is **built
    blind** (no sandbox Compliance Access Key yet) and unit-tested
    against mocks — live-verify is deferred (Track-D-D4 style).

Content vs Activity Feed
========================

Both streams share the per-tenant cursor model (``pull_state``,
distinct ``source_api``) + the 600 rpm per-parent-org budget. The
Content stream is per-MESSAGE grain (``external_id = message id``):
messages are immutable, so dedup is clean and a chat that gains new
messages re-surfaces (its ``updated_at`` advances past the cursor)
and only the new messages append. Text-first — files / generated
files / artifacts / projects are out of T5 read-first scope.

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
from typing import Any, Callable, Optional

from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

from vargate_telemetry.anthropic import (
    Activity,
    AnthropicAdminClient,
    InsufficientScope,
    admin_client_for_tenant,
    compliance_client_for_tenant,
)
from vargate_telemetry.celery_app import celery_app
from vargate_telemetry.chain import append_telemetry_record
from vargate_telemetry.db import scheduler_session_scope, session_scope
from vargate_telemetry.metering import increment
from vargate_telemetry.storage import object_store
from vargate_telemetry.storage.content import store_content


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

# Content stream (T5.2). First-run lookback for chats when no cursor
# exists. Chats are lower-volume than activities, so a wider initial
# window is fine.
DEFAULT_CONTENT_LOOKBACK_DAYS = 30
# The chats endpoint accepts 1–10 user_ids per call (Anthropic docs).
USER_IDS_PER_CHATS_CALL = 10
# Cap chats processed per invocation to bound per-tick work + respect the
# 600 rpm per-parent-org budget the content stream SHARES with the
# Activity Feed (each chat is 1 list page hit + 1 messages fetch).
# Remainder rolls forward via the persisted cursor on the next tick.
MAX_CHATS_PER_INVOCATION = 200


# ───────────────────────────────────────────────────────────────────────────
# Public exception types
# ───────────────────────────────────────────────────────────────────────────


class NotConfigured(Exception):
    """The tenant has no sealed credential for this ingest stream.

    Retained for back-compat. As of T5.2 the content pull no longer
    raises this — it **soft-skips** instead, returning
    ``{"status": "no_content_key"}`` when the tenant has no Compliance
    Access Key sealed in ``encrypted_secrets`` (so a not-yet-onboarded
    tenant doesn't generate a failed Celery task every tick). Kept as a
    public symbol in case external callers still reference it.
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
# Content stream (T5.2) — chat message text capture
#
# Enumeration chain: organizations → users → chats → messages. The chats
# endpoint requires ``user_ids[]`` (from the org-users endpoint, whose
# ``{org_uuid}`` comes from the orgs endpoint), so a Compliance Access Key
# used here needs BOTH ``read:compliance_org_data`` and
# ``read:compliance_user_data`` — confirmed at T5.1 onboarding, re-checked
# by the 403 soft-skip here. Per-message grain (external_id = message id):
# messages are immutable, so dedup is clean and a chat that gains new
# messages re-surfaces (its ``updated_at`` advances past the cursor) and
# its new messages append while the old ones dedup.
#
# Scope: chat + message TEXT only. Files / generated files / artifacts /
# projects are out of T5 read-first scope (a text-less message is skipped,
# not stored). Hard-deleted chats never appear in list_chats; soft-deleted
# ones (``deleted_at`` set) ARE captured, with the flag in metadata.
# ───────────────────────────────────────────────────────────────────────────


def _message_text(msg: Any) -> str:
    """Join the text content blocks of one chat message.

    Text-first: only ``type=='text'`` blocks with non-empty ``text``
    contribute. Files / tool blocks / artifacts are ignored here (later
    sprint). Returns ``""`` for a message with no text content.
    """
    parts = [
        b.text
        for b in (msg.content or [])
        if getattr(b, "type", None) == "text" and getattr(b, "text", None)
    ]
    return "\n\n".join(parts)


def _content_metadata(chat: Any, msg: Any) -> dict[str, Any]:
    """The searchable envelope stored alongside the encrypted message text.

    Carries chat-level context (so the T5.3 view can group messages by
    chat) + the soft-delete flag. The message TEXT itself is NOT here —
    it's the encrypted MinIO blob referenced by ``content_ref``.
    """
    md: dict[str, Any] = {
        "chat_id": chat.id,
        "message_id": msg.id,
        "role": msg.role,
    }
    if chat.name is not None:
        md["chat_name"] = chat.name
    if chat.model is not None:
        md["model"] = chat.model
    if chat.project_id is not None:
        md["project_id"] = chat.project_id
    if chat.organization_uuid is not None:
        md["organization_uuid"] = chat.organization_uuid
    if chat.user is not None:
        md["user_id"] = chat.user.id
        md["user_email"] = chat.user.email_address
    # Soft-deleted chats are captured WITH the flag (hard-deleted chats
    # never appear in list_chats at all, so they're simply absent).
    if chat.deleted_at is not None:
        md["chat_deleted_at"] = chat.deleted_at.isoformat()
    return md


def _chunks(seq: list[Any], size: int) -> Iterator[list[Any]]:
    """Yield successive ``size``-length slices of ``seq``."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _pull_content_for_tenant(
    tenant_id: str,
    *,
    since: Optional[datetime] = None,
    client: Optional[AnthropicAdminClient] = None,
    store_fn: Optional[Callable[[str, bytes], tuple[str, bytes, int]]] = None,
) -> dict[str, Any]:
    """Pull chat message content since the cursor; encrypt + persist.

    Walks orgs → users → chats → messages: enumerate every org's users,
    list each user's chats updated since the cursor, fetch each chat's
    messages, and for every not-yet-stored message store the text under
    the tenant DEK (AES-GCM → MinIO via ``store_content``) + append a
    chain-bound ``telemetry_record`` (``source_api='compliance_content'``,
    ``record_type='chat_message'``, with ``content_ref`` + ``content_hash``).

    Returns ``{"content_pulled": N, "content_deduped": M, "status": "ok"}``.

    Soft skips (return cleanly, NOT a Celery retry):
      - ``status="no_content_key"`` — no Compliance Access Key sealed
        (tenant hasn't completed the T5.1 onboarding step).
      - ``status="no_content_access"`` — the key 403s on a content /
        org-data endpoint (wrong scope or plan tier).

    ``since`` overrides the cursor (tests / re-ingest tooling). ``client``
    and ``store_fn`` are injectable seams for tests (avoid live Anthropic
    + live MinIO respectively).
    """
    if not tenant_id:
        raise ValueError("tenant_id required")

    store = store_fn if store_fn is not None else store_content

    # Build the client unless injected. No sealed key => soft skip.
    owned_client = client is None
    if owned_client:
        try:
            client = compliance_client_for_tenant(tenant_id)
        except LookupError:
            _log.info(
                "pull_content: no Compliance Access Key for %s; skipping",
                tenant_id,
            )
            return {
                "content_pulled": 0,
                "content_deduped": 0,
                "status": "no_content_key",
            }

    # Cursor is the updated_at high-water mark: a chat that gains new
    # messages re-surfaces because its updated_at advances past it.
    if since is not None:
        starting_at = since
    else:
        with session_scope(tenant_id) as s:
            cursor = _load_cursor(s, tenant_id, SOURCE_API_CONTENT)
        starting_at = cursor or (
            _now() - timedelta(days=DEFAULT_CONTENT_LOOKBACK_DAYS)
        )

    pull_started = _now()
    content_pulled = 0
    content_deduped = 0
    max_updated_seen = starting_at
    chats_processed = 0

    try:
        try:
            # 1. Enumerate every org's users -> the user_ids[] chats needs.
            user_ids: list[str] = []
            for org in client.list_organizations():
                for user in client.list_organization_users(org.uuid):
                    user_ids.append(user.id)
            user_ids = list(dict.fromkeys(user_ids))  # dedup, keep order

            # 2. List chats updated since the cursor, in <=10-user batches.
            stop = False
            for batch in _chunks(user_ids, USER_IDS_PER_CHATS_CALL):
                if stop:
                    break
                for chat in client.list_chats(
                    user_ids=batch, updated_at_gte=starting_at
                ):
                    chat_updated = chat.updated_at or chat.created_at
                    if chat_updated and chat_updated > max_updated_seen:
                        max_updated_seen = chat_updated

                    # 3. Fetch the chat's messages; store the new ones.
                    detail = client.get_chat_messages(chat.id)
                    msgs = detail.chat_messages or []
                    if msgs:
                        msg_ids = [m.id for m in msgs]
                        with session_scope(tenant_id) as s:
                            existing = {
                                r.external_id
                                for r in s.execute(
                                    sql_text(
                                        "SELECT external_id FROM "
                                        "telemetry_records WHERE "
                                        "tenant_id = :t AND source_api = :s "
                                        "AND external_id = ANY(:ids)"
                                    ),
                                    {
                                        "t": tenant_id,
                                        "s": SOURCE_API_CONTENT,
                                        "ids": msg_ids,
                                    },
                                )
                            }
                        for msg in msgs:
                            if msg.id in existing:
                                content_deduped += 1
                                continue
                            text = _message_text(msg)
                            if not text:
                                # Text-first: nothing to capture (e.g. a
                                # file-only message). Skip, don't store.
                                continue
                            content_ref, content_hash, size = store(
                                tenant_id, text.encode("utf-8")
                            )
                            try:
                                append_telemetry_record(
                                    tenant_id,
                                    record_type="chat_message",
                                    source_api=SOURCE_API_CONTENT,
                                    external_id=msg.id,
                                    occurred_at=msg.created_at,
                                    content_hash=content_hash,
                                    content_ref=content_ref,
                                    content_size_bytes=size,
                                    subject_user_id=(
                                        chat.user.id if chat.user else None
                                    ),
                                    record_metadata=_content_metadata(
                                        chat, msg
                                    ),
                                )
                                increment(tenant_id, "chat_message")
                                content_pulled += 1
                            except IntegrityError:
                                # Race: a concurrent worker inserted this
                                # message between the existence check and
                                # the append. Drop the orphan blob we just
                                # wrote and count the dedup.
                                content_deduped += 1
                                try:
                                    object_store.delete_content(
                                        tenant_id, content_ref
                                    )
                                except Exception:
                                    _log.warning(
                                        "pull_content: orphan blob cleanup "
                                        "failed for %s/%s",
                                        tenant_id,
                                        content_ref,
                                    )

                    chats_processed += 1
                    if chats_processed >= MAX_CHATS_PER_INVOCATION:
                        _log.warning(
                            "pull_content: hit per-invocation chat cap "
                            "(%d) for %s; advancing cursor, yielding to "
                            "next tick",
                            MAX_CHATS_PER_INVOCATION,
                            tenant_id,
                        )
                        stop = True
                        break
        except InsufficientScope:
            # Key lacks a compliance scope or the plan doesn't include
            # the content API. Soft skip — don't retry.
            _log.info(
                "pull_content: 403 no_content_access for %s", tenant_id
            )
            return {
                "content_pulled": 0,
                "content_deduped": 0,
                "status": "no_content_access",
            }

        # Advance the cursor. Use the max updated_at seen if we touched
        # anything; else pull_started so we don't re-query an empty window.
        new_cursor = (
            max_updated_seen
            if (content_pulled or content_deduped)
            else pull_started
        )
        with session_scope(tenant_id) as s:
            _save_cursor(
                s, tenant_id, SOURCE_API_CONTENT, new_cursor, status="ok"
            )
    finally:
        if owned_client:
            client.close()

    return {
        "content_pulled": content_pulled,
        "content_deduped": content_deduped,
        "status": "ok",
    }


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
    max_retries=3,
    name="vargate_telemetry.tasks.pull_compliance.pull_content_for_tenant",
)
def pull_content_for_tenant(self, tenant_id: str) -> dict[str, Any]:
    """Beat-dispatched per-tenant Content pull (T5.2). Retries on any
    exception OTHER than the soft skips — ``no_content_key`` (no
    Compliance Access Key sealed) and ``no_content_access`` (403) return
    cleanly, so a tenant that simply hasn't onboarded the key doesn't
    generate a failed-task every tick."""
    try:
        return _pull_content_for_tenant(tenant_id)
    except Exception as exc:
        _log.exception("pull_content failed for %s", tenant_id)
        raise self.retry(exc=exc, countdown=120)


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
    """Beat fan-out for the Content stream (T5.2).

    Enumerates active tenants and queues one ``pull_content_for_tenant``
    per row — identical fan-out shape to the Activity Feed dispatcher
    (``.delay`` to a worker, not a synchronous in-beat call). A tenant
    with no sealed Compliance Access Key soft-skips inside the per-tenant
    task (``status="no_content_key"``), so dispatching every tenant is
    cheap and correct — no failure metric, no retry, no per-tenant
    capability state to track at dispatch (the dispatch-all-with-soft-skip
    pattern; revisit at ~50 tenants).
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
        pull_content_for_tenant.delay(row.tenant_id)

    _log.info(
        "dispatch_compliance_content_pulls: queued %d tenants in region %s",
        len(rows),
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
