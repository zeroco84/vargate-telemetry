# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""OpenAI Admin audit-logs pull task (TM8 Phase B).

Pulls org-level audit events from the OpenAI Admin ``/audit_logs``
endpoint into ``telemetry_records``. Unlike usage / costs (time-window
+ ``next_page`` cursor), audit logs use a **list cursor**: the stream
advances by ``after=<last_id>``, and the stored cursor is the **last
event id ingested** (a string), NOT a timestamp.

Two Celery tasks compose the pipeline:

  - ``dispatch_openai_audit_pulls`` — beat fan-out over active tenants.
  - ``pull_openai_audit_for_tenant`` — per-tenant. Loads the
    ``(tenant, "openai_audit_logs")`` cursor (last id), iterates
    ``client.list_audit_logs(after=<cursor>)``, normalizes each
    ``AuditLogEntry`` to a record, and advances the cursor to the last
    id seen.

Empty is NORMAL (recon §1/§8)
=============================

Audit logging is effectively Enterprise/org-policy gated — the endpoint
returns ``200`` with an empty ``data`` list on a PAYG org (accessible ≠
populated). An empty pull is therefore the steady state for most
tenants and is NOT an error: we return ``status="no_audit_data"`` and
leave the cursor where it was (there's no new id to advance to). A 403
(genuinely scope-limited key) is the separate soft-skip path
(``status="no_openai_audit_access"``).

``external_id`` (recon-pinned)::

    openai:openai_audit_logs:{event_id}

The OpenAI event ``id`` is globally stable, so it IS the dedup key — no
window needed. A re-pull that overlaps an already-ingested id dedups on
the UNIQUE constraint.

``subject_user_id`` is the actor's id when present, else the actor's
email (recon §8: the actor shape carries a nested ``session`` /
``api_key`` whose detail varies; we read what's there and store the
full entry in ``record_metadata``). The email (when present) also goes
into ``metadata.user_email`` for the cross-vendor reconciler, mirroring
the usage path — though audit-log attribution is best-effort.

Cursor semantics
================

The list cursor is "what's the newest id we've ingested." Because
``client.list_audit_logs`` walks ``after=<last_id>`` forward, passing
the stored cursor as ``after`` resumes from just past the last ingested
event. We advance the cursor to the last id of THIS pull only if we saw
at least one event; an empty pull leaves it untouched.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

from vargate_telemetry.celery_app import celery_app
from vargate_telemetry.chain import append_telemetry_record
from vargate_telemetry.db import scheduler_session_scope, session_scope
from vargate_telemetry.metering import increment
from vargate_telemetry.openai import (
    AuditLogEntry,
    InsufficientScope,
    OpenAIAdminClient,
    admin_client_for_tenant,
)

_log = logging.getLogger(__name__)

# Source-API name used in pull_state + telemetry_records for this stream.
SOURCE_API_OPENAI_AUDIT = "openai_audit_logs"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ───────────────────────────────────────────────────────────────────────────
# Cursor I/O — the cursor here is a STRING last-id, not a timestamp, so
# these are deliberately NOT the timestamp helpers from the usage/costs
# tasks (they round-trip via datetime.fromisoformat). Stored verbatim in
# pull_state.cursor (a text column).
# ───────────────────────────────────────────────────────────────────────────


def _load_cursor(session, tenant_id: str, source_api: str) -> Optional[str]:
    row = session.execute(
        sql_text(
            "SELECT cursor FROM pull_state "
            "WHERE tenant_id = :t AND source_api = :s"
        ),
        {"t": tenant_id, "s": source_api},
    ).first()
    if row is None or row.cursor is None:
        return None
    return row.cursor


def _save_cursor(
    session,
    tenant_id: str,
    source_api: str,
    cursor: Optional[str],
    *,
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
            "c": cursor,
            "now": _now(),
            "status": status,
            "err": error,
        },
    )


def _actor_identity(entry: AuditLogEntry) -> tuple[Optional[str], Optional[str]]:
    """Return ``(subject_user_id, email)`` for an audit entry.

    Recon §8: the org's audit feed was empty, so the actor shape is
    modeled from docs (a ``type`` discriminator with nested
    ``session`` / ``api_key`` detail). We dig the most useful
    identifier out defensively:

      - the session actor's user id / email (``actor.session.user.{id,
        email}``), or
      - the api_key actor's id (``actor.api_key.id``).

    ``subject_user_id`` prefers the user id, falling back to the email
    (so a record always has *some* actor identifier when one exists);
    ``email`` is returned separately for the cross-vendor match key.
    Everything is read from ``model_extra``-friendly dicts so a novel
    actor variant doesn't crash — it just yields ``(None, None)`` and
    the record lands actor-less.
    """
    actor = entry.actor
    if actor is None:
        return None, None

    user_id: Optional[str] = None
    email: Optional[str] = None

    session = actor.session or {}
    if isinstance(session, dict):
        user = session.get("user")
        if isinstance(user, dict):
            user_id = user.get("id") or user_id
            email = user.get("email") or email

    api_key = actor.api_key or {}
    if isinstance(api_key, dict):
        # api_key actors expose an id; some shapes nest a user too.
        if user_id is None:
            user_id = api_key.get("id")
        nested_user = api_key.get("user")
        if isinstance(nested_user, dict):
            user_id = user_id or nested_user.get("id")
            email = email or nested_user.get("email")

    subject = user_id or email
    return subject, email


def _normalize_audit(entry: AuditLogEntry) -> dict[str, Any]:
    """Turn one ``AuditLogEntry`` into ``telemetry_records`` insert kwargs.

    ``external_id`` = ``openai:openai_audit_logs:{event_id}`` (the event
    id is the stable dedup key — no window needed).

    ``content_hash`` is SHA-256 over the canonical JSON of the full
    entry; the entire entry (event-type-specific detail included via
    ``extra="allow"``) lands in ``record_metadata`` without branching.

    ``subject_user_id`` is the actor's id-or-email; the email (when
    present) is also surfaced as ``metadata.user_email`` for the
    reconciler.
    """
    entry_dict = entry.model_dump(mode="json")
    subject_user_id, email = _actor_identity(entry)

    sub_meta: dict[str, Any] = {
        "entry": entry_dict,
        # Top-level operational fields for the dashboard / reconciler.
        "event_type": entry.type,
        "subject_user_id": subject_user_id,
    }
    if email is not None:
        sub_meta["user_email"] = email

    canonical = json.dumps(
        sub_meta, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    return {
        "record_type": "audit_log",
        "source_api": SOURCE_API_OPENAI_AUDIT,
        "external_id": f"openai:{SOURCE_API_OPENAI_AUDIT}:{entry.id}",
        "occurred_at": entry.effective_at,
        "content_hash": hashlib.sha256(canonical).digest(),
        "record_metadata": sub_meta,
        "subject_user_id": subject_user_id,
    }


def _pull_openai_audit_for_tenant(
    tenant_id: str,
    *,
    client: Optional[OpenAIAdminClient] = None,
) -> dict[str, Any]:
    """Pure-Python pull. Returns counts + status.

    Happy path (events ingested)::

        {"records_pulled": N, "records_deduped": M, "status": "ok"}

    Empty feed (NORMAL — accessible-but-unpopulated tier)::

        {"records_pulled": 0, "records_deduped": 0,
         "status": "no_audit_data"}

    403 soft-skip (scope-limited key)::

        {"records_pulled": 0, "records_deduped": 0,
         "status": "no_openai_audit_access"}
    """
    if not tenant_id:
        raise ValueError("tenant_id required")

    with session_scope(tenant_id) as s:
        cursor = _load_cursor(s, tenant_id, SOURCE_API_OPENAI_AUDIT)

    owned_client = client is None
    if owned_client:
        try:
            client = admin_client_for_tenant(tenant_id)
        except LookupError:
            # No OpenAI admin key sealed — soft-skip (the dispatcher fans
            # out to ALL active tenants; most have no OpenAI key). Cursor
            # untouched, no retry.
            _log.debug(
                "pull_openai_audit: no openai key sealed for %s", tenant_id
            )
            return {
                "records_pulled": 0,
                "records_deduped": 0,
                "status": "no_openai_key",
            }

    records_pulled = 0
    records_deduped = 0
    last_id: Optional[str] = None

    try:
        try:
            for entry in client.list_audit_logs(after=cursor):
                # Track the newest id we've seen so the cursor advances
                # past it. The list walks forward (after=<last_id>), so
                # the final entry of the final page is the newest.
                last_id = entry.id
                fields = _normalize_audit(entry)
                try:
                    append_telemetry_record(tenant_id, **fields)
                    increment(tenant_id, SOURCE_API_OPENAI_AUDIT)
                    records_pulled += 1
                except IntegrityError:
                    records_deduped += 1
                    _log.info(
                        "pull_openai_audit: dedup hit %s/%s",
                        tenant_id,
                        fields["external_id"],
                    )
        except InsufficientScope:
            _log.info(
                "pull_openai_audit: 403 no_openai_audit_access for %s",
                tenant_id,
            )
            return {
                "records_pulled": 0,
                "records_deduped": 0,
                "status": "no_openai_audit_access",
            }

        # Empty feed: NORMAL on accessible-but-unpopulated tiers. No new
        # id to advance to, so leave the cursor untouched and report
        # no_audit_data (distinct from the 403 no-access path).
        if last_id is None:
            _log.info(
                "pull_openai_audit: empty feed for %s (no_audit_data)",
                tenant_id,
            )
            return {
                "records_pulled": 0,
                "records_deduped": 0,
                "status": "no_audit_data",
            }

        # Advance the cursor to the last id seen (even on a dedup-only
        # run — we still saw ids, the window's just already ingested).
        with session_scope(tenant_id) as s:
            _save_cursor(
                s,
                tenant_id,
                SOURCE_API_OPENAI_AUDIT,
                last_id,
                status="ok",
            )
    finally:
        if owned_client:
            client.close()

    return {
        "records_pulled": records_pulled,
        "records_deduped": records_deduped,
        "status": "ok",
    }


@celery_app.task(
    bind=True,
    max_retries=3,
    name=(
        "vargate_telemetry.tasks.pull_openai_audit."
        "pull_openai_audit_for_tenant"
    ),
)
def pull_openai_audit_for_tenant(self, tenant_id: str) -> dict[str, Any]:
    """Beat-dispatched per-tenant audit pull. Retries on any exception
    OTHER than the 403 soft-skip / empty-feed (which return cleanly)."""
    try:
        return _pull_openai_audit_for_tenant(tenant_id)
    except Exception as exc:
        _log.exception("pull_openai_audit failed for %s", tenant_id)
        raise self.retry(exc=exc, countdown=120)


@celery_app.task(
    name=(
        "vargate_telemetry.tasks.pull_openai_audit."
        "dispatch_openai_audit_pulls"
    ),
)
def dispatch_openai_audit_pulls(region: Optional[str] = None) -> int:
    """Beat fan-out. Enumerate active tenants; queue one audit pull each.

    Mirrors ``pull_admin.dispatch_admin_pulls`` — all regions by
    default (TM5 T5.0 region-gap fix). The per-tenant task soft-skips
    on 403 and treats an empty feed as normal.
    """
    with scheduler_session_scope() as s:
        if region is None:
            rows = s.execute(
                sql_text(
                    "SELECT tenant_id FROM tenants WHERE active = true"
                )
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
        pull_openai_audit_for_tenant.delay(row.tenant_id)

    _log.info(
        "dispatch_openai_audit_pulls: queued %d tenants in region %s",
        len(rows),
        region or "all",
    )
    return len(rows)


__all__ = [
    "SOURCE_API_OPENAI_AUDIT",
    "_pull_openai_audit_for_tenant",
    "dispatch_openai_audit_pulls",
    "pull_openai_audit_for_tenant",
]
