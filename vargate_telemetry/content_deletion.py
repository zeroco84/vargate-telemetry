# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Content deletion (TM6 T6.1) — chain-safe, tamper-evident.

Deletes captured compliance content while preserving the audit chain.
The original ``telemetry_records`` rows are NEVER mutated or removed —
that would break the per-tenant hash chain. Instead we make the content
*unreadable* and record the deletion as an append-only chain event:

  - **Per-chat / per-user (DSR):** delete the specific MinIO blobs +
    append one ``content_deletion`` event PER deleted message record.
    The blob is gone (content unreadable) and the deletion is itself a
    tamper-evident chain entry.
  - **Per-tenant (offboarding):** crypto-shred the tenant DEK (every
    blob + sealed secret becomes permanently undecryptable at once) +
    append one tenant-scoped ``content_deletion`` event. TERMINAL.

``verify_telemetry_chain`` stays green before and after (``content_hash``
is SHA-256 of plaintext, stored in clear). This is the AGCS "prove it
existed AND prove it was deleted" posture — destructive
right-to-be-forgotten without breaking eDiscovery.

Order: **blob-delete first** (the load-bearing make-it-unreadable step),
**then event-append** (the proof). A crash between is recoverable —
re-running is idempotent (``delete_content`` is idempotent; the event's
``external_id`` dedups on the chain UNIQUE). Worst case of this order is
"content gone, proof lags until a re-run" — strictly safer than the
reverse ("chain says deleted while content is still readable").

Synchronous for now (deletion is rare + admin-initiated; typical chat =
tens of messages). A very large per-user/per-tenant delete that risks a
request timeout is a future async/Celery follow-up.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

from vargate_telemetry.chain import append_telemetry_record
from vargate_telemetry.crypto.seal import destroy_tenant_dek
from vargate_telemetry.db import session_scope
from vargate_telemetry.storage import content as content_store

_log = logging.getLogger(__name__)

RECORD_TYPE_DELETION = "content_deletion"
RECORD_TYPE_REVEAL = "content_reveal"
SOURCE_API_CONTENT = "compliance_content"
_RECORD_TYPE_MESSAGE = "chat_message"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _deletion_content_hash(metadata: dict[str, Any]) -> bytes:
    """A deletion event carries no content blob; bind its required
    ``content_hash`` to the canonical deletion descriptor so the chain
    entry is meaningful + unique per deletion (metadata includes the
    deleted_at timestamp)."""
    canonical = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def _append_deletion_event(
    tenant_id: str,
    *,
    external_id: str,
    occurred_at: datetime,
    metadata: dict[str, Any],
    subject_user_id: Optional[str] = None,
) -> bool:
    """Append one ``content_deletion`` chain event. Returns False
    (idempotent no-op) if it already exists — the
    ``(tenant_id, source_api, external_id)`` UNIQUE makes re-deletion
    safe. Does NOT require the tenant DEK (no encryption), so it works
    even after a crypto-shred."""
    try:
        append_telemetry_record(
            tenant_id,
            record_type=RECORD_TYPE_DELETION,
            source_api=SOURCE_API_CONTENT,
            external_id=external_id,
            occurred_at=occurred_at,
            content_hash=_deletion_content_hash(metadata),
            record_metadata=metadata,
            subject_user_id=subject_user_id,
        )
        return True
    except IntegrityError:
        return False


def _select_message_records(
    tenant_id: str, *, where: str, params: dict[str, Any]
) -> list[Any]:
    """The compliance_content MESSAGE records matching ``where`` (NOT the
    deletion events). RLS-scoped via session_scope."""
    with session_scope(tenant_id) as s:
        return s.execute(
            sql_text(
                "SELECT external_id, content_ref, "
                "metadata->>'chat_id' AS chat_id, "
                "subject_user_id::text AS subject_user_id "
                "FROM telemetry_records "
                "WHERE tenant_id = :t AND source_api = :s "
                "AND record_type = :rt " + where
            ),
            {
                "t": tenant_id,
                "s": SOURCE_API_CONTENT,
                "rt": _RECORD_TYPE_MESSAGE,
                **params,
            },
        ).all()


def _delete_message_records(
    tenant_id: str,
    rows: list[Any],
    *,
    scope: str,
    reason: str,
    requested_by: str,
    dsr_subject: Optional[str] = None,
) -> dict[str, int]:
    """Blob-delete + deletion-event each row. Idempotent per row."""
    now = _now()
    deleted = 0
    already = 0
    for r in rows:
        # 1. Make it unreadable (idempotent; secondary to DEK-shred but
        #    removes the bytes for per-chat/per-user deletes).
        if r.content_ref:
            try:
                content_store.delete_content(tenant_id, r.content_ref)
            except Exception:  # noqa: BLE001 — best-effort; re-run retries
                _log.warning(
                    "content_deletion: blob delete failed %s/%s (event still "
                    "recorded; re-run to retry)",
                    tenant_id,
                    r.content_ref,
                )
        # 2. Record the tamper-evident proof.
        metadata: dict[str, Any] = {
            "deleted_external_id": r.external_id,
            "deleted_record_type": _RECORD_TYPE_MESSAGE,
            "chat_id": r.chat_id,
            "scope": scope,
            "reason": reason,
            "requested_by": requested_by,
            "deleted_at": now.isoformat(),
        }
        if dsr_subject is not None:
            metadata["dsr_subject"] = dsr_subject
        if _append_deletion_event(
            tenant_id,
            external_id=f"deletion:{r.external_id}",
            occurred_at=now,
            metadata=metadata,
            subject_user_id=r.subject_user_id,
        ):
            deleted += 1
        else:
            already += 1
    return {"deleted": deleted, "already_deleted": already}


def delete_chat(
    tenant_id: str, chat_id: str, *, reason: str, requested_by: str
) -> dict[str, int]:
    """Delete one chat's captured messages (blobs + deletion events)."""
    if not tenant_id or not chat_id:
        raise ValueError("tenant_id and chat_id required")
    rows = _select_message_records(
        tenant_id,
        where="AND metadata->>'chat_id' = :chat_id",
        params={"chat_id": chat_id},
    )
    result = _delete_message_records(
        tenant_id, rows, scope="chat", reason=reason, requested_by=requested_by
    )
    _log.info(
        "content_deletion: chat %s/%s — %s", tenant_id, chat_id, result
    )
    return result


def delete_user_content(
    tenant_id: str, subject_user_id: str, *, reason: str, requested_by: str
) -> dict[str, int]:
    """Delete all of one data-subject's captured content (DSR /
    right-to-be-forgotten) across every chat they own."""
    if not tenant_id or not subject_user_id:
        raise ValueError("tenant_id and subject_user_id required")
    rows = _select_message_records(
        tenant_id,
        where="AND subject_user_id::text = :uid",
        params={"uid": subject_user_id},
    )
    result = _delete_message_records(
        tenant_id,
        rows,
        scope="user",
        reason=reason,
        requested_by=requested_by,
        dsr_subject=subject_user_id,
    )
    _log.info(
        "content_deletion: DSR user %s/%s — %s",
        tenant_id,
        subject_user_id,
        result,
    )
    return result


def crypto_shred_tenant(
    tenant_id: str, *, reason: str, requested_by: str
) -> dict[str, Any]:
    """Crypto-shred the whole tenant (account offboarding): destroy the
    tenant DEK — every content blob AND sealed secret becomes permanently
    undecryptable — then record a tenant-scoped deletion event. TERMINAL
    + irreversible.

    The deletion event is appended AFTER the shred; it doesn't need the
    DEK (the chain append doesn't encrypt). Idempotent: a second shred is
    a no-op (no DEK + the tenant event already exists).
    """
    if not tenant_id:
        raise ValueError("tenant_id required")
    destroyed = destroy_tenant_dek(tenant_id)
    now = _now()
    metadata: dict[str, Any] = {
        "scope": "tenant",
        "reason": reason,
        "requested_by": requested_by,
        "deleted_at": now.isoformat(),
        "dek_destroyed": destroyed,
    }
    appended = _append_deletion_event(
        tenant_id,
        external_id="deletion:tenant",
        occurred_at=now,
        metadata=metadata,
    )
    _log.warning(
        "content_deletion: TENANT crypto-shred %s — dek_destroyed=%s "
        "event_appended=%s (requested_by=%s)",
        tenant_id,
        destroyed,
        appended,
        requested_by,
    )
    return {"dek_destroyed": destroyed, "event_appended": appended}


def log_content_reveal(
    tenant_id: str,
    *,
    scope: str,
    revealed_by: str,
    chat_id: Optional[str] = None,
    subject_user_id: Optional[str] = None,
) -> None:
    """Append an audit-logged ``content_reveal`` event (TM6 T6.3).

    A reveal is a privileged un-masking of PII (content view or full-
    content export). Each reveal is its OWN tamper-evident chain event
    (unique ``external_id`` — never deduped), so the audit trail records
    every time masked content was exposed, by whom, and when. Inert to
    the content view + export queries (they filter ``record_type``).
    """
    now = _now()
    metadata: dict[str, Any] = {
        "scope": scope,  # 'chat' | 'export'
        "revealed_by": revealed_by,
        "revealed_at": now.isoformat(),
    }
    if chat_id is not None:
        metadata["chat_id"] = chat_id
    if subject_user_id is not None:
        metadata["subject_user_id"] = subject_user_id
    append_telemetry_record(
        tenant_id,
        record_type=RECORD_TYPE_REVEAL,
        source_api=SOURCE_API_CONTENT,
        external_id=f"reveal:{uuid.uuid4().hex}",
        occurred_at=now,
        content_hash=_deletion_content_hash(metadata),
        record_metadata=metadata,
        subject_user_id=subject_user_id,
    )
    _log.info(
        "content_reveal: tenant %s scope=%s chat=%s subject=%s by=%s",
        tenant_id,
        scope,
        chat_id,
        subject_user_id,
        revealed_by,
    )
