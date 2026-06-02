# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Compliance content view (TM5 T5.3) — read-only.

The dashboard surface for content captured by the T5.2 pull task. Two
read endpoints over the ``compliance_content`` telemetry records:

  - ``GET /content/chats`` — list captured chats for the authenticated
    tenant, aggregated from the per-message records by ``chat_id``
    (metadata). Metadata only — no decryption on the list.
  - ``GET /content/chats/{chat_id}`` — one chat's messages with the
    text **decrypted on read** (the MinIO blob is fetched + AES-GCM
    decrypted under the tenant DEK at request time; plaintext is never
    persisted in the clear). 404 if no such chat for the tenant.

Read-only: no delete, no redaction, no export endpoint (the dashboard
exports client-side from the detail response). All queries run under
``session_scope(tenant_id)`` so RLS enforces tenant isolation — a
caller who guesses another tenant's chat_id gets a 404 (zero rows under
their RLS view), same posture as the Sessions detail endpoint.

Capability: the dashboard gates the page on ``content_capture``; the
endpoints themselves just require an authenticated, tenant-bound user
(``current_user``) — a tenant with no key simply has no
``compliance_content`` records, so the list is empty.

Build-blind (TM5): there's no live content yet (no Compliance Access
Key), so these are unit-tested against synthetic records + a stubbed
content retriever. The decrypt-on-read path is also covered by a real
``store_content``↔``retrieve_content`` round-trip in the pull tests.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text

from vargate_telemetry import content_deletion
from vargate_telemetry.auth.middleware import AuthenticatedUser, current_user
from vargate_telemetry.auth.roles import require_admin
from vargate_telemetry.db import session_scope

_log = logging.getLogger(__name__)

router = APIRouter()

# Source-API the content stream lands under (mirrors
# pull_compliance.SOURCE_API_CONTENT — duplicated as a literal here to
# avoid importing the Celery task module into the API layer).
_SOURCE_API_CONTENT = "compliance_content"

# Cap the chat list to bound the response. Read-only first pass — content
# volume per tenant is small; cursor pagination is a follow-up if a
# tenant accumulates more than this many distinct chats.
_MAX_CHATS = 200


# ───────────────────────────────────────────────────────────────────────────
# Response shapes
# ───────────────────────────────────────────────────────────────────────────


class ChatSummary(BaseModel):
    chat_id: str
    chat_name: Optional[str] = None
    model: Optional[str] = None
    user_email: Optional[str] = None
    message_count: int
    first_message_at: datetime
    last_message_at: datetime
    # True if the chat was soft-deleted in claude.ai (still captured).
    deleted: bool = False
    # True if the chat's content was purged via Ogma (DSR / per-chat
    # delete / tenant crypto-shred) — distinct from `deleted` (which is
    # claude.ai's own soft-delete). The chain records remain; the blobs
    # are gone. Renders as a tombstone, not a readable row.
    purged: bool = False


class ChatListResponse(BaseModel):
    chats: list[ChatSummary]
    # True when the tenant has more than `_MAX_CHATS` distinct chats and
    # the list was capped (so the UI can show "showing the most recent N"
    # rather than implying completeness — no silent truncation).
    truncated: bool = False


class ContentMessage(BaseModel):
    record_id: str
    message_id: str
    role: str
    occurred_at: datetime
    # The decrypted message text. Null if the blob couldn't be decrypted
    # (tamper / missing / transport error) — the message still renders.
    content: Optional[str] = None
    content_size_bytes: Optional[int] = None
    # True if this message's content was purged via Ogma; `content` is
    # null because the blob is gone (the chain record itself remains).
    purged: bool = False


class ChatDetailResponse(BaseModel):
    chat_id: str
    chat_name: Optional[str] = None
    model: Optional[str] = None
    user_email: Optional[str] = None
    deleted: bool = False
    # Chat-level purge tombstone (set when the whole chat was deleted or
    # the tenant crypto-shredded). `purge_reason` / `purged_at` come off
    # the most recent content_deletion event covering the chat.
    purged: bool = False
    purge_reason: Optional[str] = None
    purged_at: Optional[datetime] = None
    messages: list[ContentMessage]


# ── Deletion (T6.1) request / response shapes ──────────────────────────────


class DeletionRequest(BaseModel):
    """A reason is mandatory — a deletion with no recorded justification
    is exactly what an auditor flags. Goes into the chain event."""

    reason: str = Field(..., min_length=1, max_length=500)


class DeletionResult(BaseModel):
    scope: str
    # Records whose deletion event was newly appended this call.
    deleted: int
    # Records already deleted on a prior call (idempotent re-delete).
    already_deleted: int


class TenantShredRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)
    # Typed confirmation: must equal the caller's own tenant_id. Guards
    # the terminal, irreversible crypto-shred against a fat-finger.
    confirm_tenant_id: str = Field(..., min_length=1)


class TenantShredResult(BaseModel):
    dek_destroyed: bool
    event_appended: bool


# ───────────────────────────────────────────────────────────────────────────
# Content-decryption injection seam (mirrors sessions.py) — production
# wires retrieve_content; tests stub it to avoid live MinIO + HSM.
# ───────────────────────────────────────────────────────────────────────────


def _default_content_retriever(tenant_id: str, content_ref: str) -> bytes:
    from vargate_telemetry.storage import content as content_mod

    return content_mod.retrieve_content(tenant_id, content_ref)


_content_retriever = _default_content_retriever


def set_content_retriever_for_test(retriever: Optional[Any]) -> None:
    """Test hook: substitute the content-blob decrypt function. Pass
    ``None`` to reset."""
    global _content_retriever
    _content_retriever = (
        retriever if retriever is not None else _default_content_retriever
    )


def _require_tenant(user: AuthenticatedUser) -> str:
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "no_tenant_bound",
                "message": "Your session is not bound to a tenant yet.",
            },
        )
    return user.tenant_id


# ───────────────────────────────────────────────────────────────────────────
# GET /content/chats
# ───────────────────────────────────────────────────────────────────────────


@router.get(
    "/content/chats",
    response_model=ChatListResponse,
    operation_id="listContentChats",
    tags=["content"],
    summary="List captured compliance-content chats for the tenant",
)
def list_content_chats(
    user: AuthenticatedUser = Depends(current_user),
) -> ChatListResponse:
    """Aggregate the per-message ``compliance_content`` records into one
    row per ``chat_id`` (newest activity first). Metadata only — no
    decryption here; that happens in the per-chat detail endpoint."""
    tenant_id = _require_tenant(user)

    # `_MAX_CHATS + 1` so we can tell the caller the list was capped.
    # `all_purged` LEFT-JOINs each message against its per-message
    # content_deletion event (external_id = 'deletion:' || <msg id>) and
    # is true only when EVERY message in the chat has one — the SAME
    # "fully purged" definition the detail endpoint uses (all(m.purged)).
    # Marking a chat purged on ANY deletion event would disagree with the
    # detail view for a partially-deleted chat (e.g. a per-user DSR that
    # touches only some messages of a multi-subject chat).
    sql = """
        SELECT
            m.metadata->>'chat_id'                       AS chat_id,
            MAX(m.metadata->>'chat_name')                AS chat_name,
            MAX(m.metadata->>'model')                    AS model,
            MAX(m.metadata->>'user_email')               AS user_email,
            bool_or(m.metadata->>'chat_deleted_at' IS NOT NULL) AS deleted,
            bool_and(d.external_id IS NOT NULL)          AS all_purged,
            COUNT(*)                                     AS message_count,
            MIN(m.occurred_at)                           AS first_message_at,
            MAX(m.occurred_at)                           AS last_message_at
        FROM telemetry_records m
        LEFT JOIN telemetry_records d
            ON d.source_api = :src
           AND d.record_type = 'content_deletion'
           AND d.external_id = 'deletion:' || m.external_id
        WHERE m.tenant_id = current_setting('app.tenant_id')
          AND m.source_api = :src
          AND m.record_type = 'chat_message'
          AND m.metadata->>'chat_id' IS NOT NULL
        GROUP BY m.metadata->>'chat_id'
        ORDER BY last_message_at DESC
        LIMIT :lim
    """
    # A tenant crypto-shred has no per-message events — it's one
    # scope='tenant' event that purges everything at once.
    tenant_shred_sql = """
        SELECT bool_or(metadata->>'scope' = 'tenant') AS tenant_shred
        FROM telemetry_records
        WHERE tenant_id = current_setting('app.tenant_id')
          AND source_api = :src
          AND record_type = 'content_deletion'
    """
    with session_scope(tenant_id) as s:
        rows = s.execute(
            sql_text(sql),
            {"src": _SOURCE_API_CONTENT, "lim": _MAX_CHATS + 1},
        ).all()
        tenant_shred = bool(
            s.execute(
                sql_text(tenant_shred_sql), {"src": _SOURCE_API_CONTENT}
            ).scalar()
        )

    truncated = len(rows) > _MAX_CHATS
    if truncated:
        _log.info(
            "list_content_chats: tenant %s has >%d chats; list capped",
            tenant_id,
            _MAX_CHATS,
        )
    page = rows[:_MAX_CHATS]

    chats = [
        ChatSummary(
            chat_id=r.chat_id,
            chat_name=r.chat_name,
            model=r.model,
            user_email=r.user_email,
            message_count=int(r.message_count),
            first_message_at=r.first_message_at,
            last_message_at=r.last_message_at,
            deleted=bool(r.deleted),
            purged=tenant_shred or bool(r.all_purged),
        )
        for r in page
    ]
    return ChatListResponse(chats=chats, truncated=truncated)


# ───────────────────────────────────────────────────────────────────────────
# GET /content/chats/{chat_id}
# ───────────────────────────────────────────────────────────────────────────


@router.get(
    "/content/chats/{chat_id}",
    response_model=ChatDetailResponse,
    operation_id="getContentChatDetail",
    tags=["content"],
    summary="Get one captured chat with its messages decrypted on read",
)
def get_content_chat_detail(
    chat_id: str = Path(..., min_length=1),
    user: AuthenticatedUser = Depends(current_user),
) -> ChatDetailResponse:
    tenant_id = _require_tenant(user)

    sql = """
        SELECT
            id::text          AS record_id,
            external_id       AS message_id,
            occurred_at,
            metadata,
            content_ref,
            content_size_bytes
        FROM telemetry_records
        WHERE tenant_id = current_setting('app.tenant_id')
          AND source_api = :src
          AND record_type = 'chat_message'
          AND metadata->>'chat_id' = :chat_id
        ORDER BY occurred_at, chain_seq
    """
    # content_deletion events covering this chat (per-message) or the
    # whole tenant (crypto-shred) → drives per-message + chat tombstones.
    # `purge_reason` is taken from the MOST RECENT covering event (by
    # deleted_at), not MAX(reason) which would pick a lexicographically
    # arbitrary string when several deletions touched the chat.
    purge_sql = """
        SELECT
            bool_or(metadata->>'scope' = 'tenant')                  AS tenant_shred,
            array_remove(array_agg(metadata->>'deleted_external_id'), NULL) AS purged_eids,
            (array_agg(metadata->>'reason'
                       ORDER BY metadata->>'deleted_at' DESC))[1]   AS purge_reason,
            MAX(metadata->>'deleted_at')                            AS purged_at
        FROM telemetry_records
        WHERE tenant_id = current_setting('app.tenant_id')
          AND source_api = :src
          AND record_type = 'content_deletion'
          AND (metadata->>'chat_id' = :chat_id OR metadata->>'scope' = 'tenant')
    """
    with session_scope(tenant_id) as s:
        rows = s.execute(
            sql_text(sql),
            {"src": _SOURCE_API_CONTENT, "chat_id": chat_id},
        ).all()
        purge = s.execute(
            sql_text(purge_sql),
            {"src": _SOURCE_API_CONTENT, "chat_id": chat_id},
        ).one()

    if not rows:
        # Real-but-other-tenant (RLS hides it) or never existed — both
        # 404 so the distinction can't be probed cross-tenant.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "chat_not_found",
                "message": "No captured chat with that id for your tenant.",
            },
        )

    tenant_shred = bool(purge.tenant_shred)
    purged_eids = set(purge.purged_eids or [])

    # Chat-level fields come off any record's metadata (every message of
    # a chat carries the same chat envelope — see pull_compliance).
    head = rows[0].metadata or {}
    messages: list[ContentMessage] = []
    for r in rows:
        md = r.metadata or {}
        msg_purged = tenant_shred or r.message_id in purged_eids
        content_plaintext: Optional[str] = None
        # Skip the decrypt for a purged message — the blob is gone, so the
        # attempt would only fail loudly. A purged message NEVER returns
        # content (belt-and-braces even if a blob lingered).
        if r.content_ref and not msg_purged:
            try:
                blob = _content_retriever(tenant_id, r.content_ref)
                content_plaintext = blob.decode("utf-8", errors="replace")
            except Exception:
                # IntegrityError / NotFound / transport — surface as null
                # content (the message still renders) + log loudly.
                _log.exception(
                    "content_detail: decrypt failed for %s/%s",
                    tenant_id,
                    r.content_ref,
                )
                content_plaintext = None
        messages.append(
            ContentMessage(
                record_id=r.record_id,
                message_id=r.message_id,
                role=md.get("role") or "unknown",
                occurred_at=r.occurred_at,
                content=content_plaintext,
                content_size_bytes=r.content_size_bytes,
                purged=msg_purged,
            )
        )

    chat_purged = tenant_shred or (
        bool(messages) and all(m.purged for m in messages)
    )
    return ChatDetailResponse(
        chat_id=chat_id,
        chat_name=head.get("chat_name"),
        model=head.get("model"),
        user_email=head.get("user_email"),
        deleted=head.get("chat_deleted_at") is not None,
        purged=chat_purged,
        purge_reason=purge.purge_reason if chat_purged else None,
        purged_at=purge.purged_at if chat_purged else None,
        messages=messages,
    )


# ───────────────────────────────────────────────────────────────────────────
# Content deletion (T6.1) — admin-only, chain-safe. Each path deletes the
# blob (or crypto-shreds) + appends an append-only content_deletion event;
# the original chain records are NEVER mutated or removed. Idempotent.
# ───────────────────────────────────────────────────────────────────────────


@router.delete(
    "/content/chats/{chat_id}",
    response_model=DeletionResult,
    operation_id="deleteContentChat",
    tags=["content"],
    summary="Delete one captured chat's content (admin)",
)
def delete_content_chat(
    chat_id: str = Path(..., min_length=1),
    req: DeletionRequest = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
) -> DeletionResult:
    """Delete the chat's message blobs + append a content_deletion event
    per message. The chain records remain (immutable); the content is
    gone. Idempotent — re-deleting reports ``already_deleted``."""
    tenant_id = _require_tenant(user)
    result = content_deletion.delete_chat(
        tenant_id, chat_id, reason=req.reason, requested_by=user.user_id
    )
    return DeletionResult(scope="chat", **result)


@router.delete(
    "/content/users/{subject_user_id}",
    response_model=DeletionResult,
    operation_id="deleteUserContent",
    tags=["content"],
    summary="Delete all of one user's captured content — DSR (admin)",
)
def delete_user_content_endpoint(
    subject_user_id: str = Path(..., min_length=1),
    req: DeletionRequest = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
) -> DeletionResult:
    """Right-to-be-forgotten for one data subject: delete every blob of
    theirs across all chats + append content_deletion events."""
    tenant_id = _require_tenant(user)
    result = content_deletion.delete_user_content(
        tenant_id,
        subject_user_id,
        reason=req.reason,
        requested_by=user.user_id,
    )
    return DeletionResult(scope="user", **result)


@router.post(
    "/content/tenant/shred",
    response_model=TenantShredResult,
    operation_id="cryptoShredTenant",
    tags=["content"],
    summary="Crypto-shred ALL tenant content — terminal offboarding (admin)",
)
def crypto_shred_tenant_endpoint(
    req: TenantShredRequest = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
) -> TenantShredResult:
    """Destroy the tenant DEK — ALL content + sealed secrets become
    permanently undecryptable — and record a tenant-scoped deletion
    event. TERMINAL + irreversible. Requires ``confirm_tenant_id`` to
    equal the caller's own tenant_id (typed confirmation)."""
    tenant_id = _require_tenant(user)
    if req.confirm_tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "confirm_mismatch",
                "message": (
                    "confirm_tenant_id must equal your tenant_id to "
                    "authorise this terminal, irreversible crypto-shred."
                ),
            },
        )
    result = content_deletion.crypto_shred_tenant(
        tenant_id, reason=req.reason, requested_by=user.user_id
    )
    return TenantShredResult(**result)
