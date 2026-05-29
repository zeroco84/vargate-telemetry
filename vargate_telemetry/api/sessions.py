# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Sessions API (T5.5) — first dashboard view of ingested content.

A **Session** is one ``(date, actor)`` tuple aggregated across the
``code_analytics`` and ``compliance_activities`` streams. Admin API
usage records are bucket-grain (not actor-grain) and don't roll up
into Sessions.

Endpoints
=========

  - ``GET /sessions`` — paginated list of Sessions for the
    authenticated tenant. Cursor pagination, filters by
    ``source_api`` / ``actor_key`` / ``since`` / ``until``.
  - ``GET /sessions/{session_id}`` — detail view for one Session,
    returning every audit-chain record folded into it (in chain
    order). Content-blob decryption happens here when records carry
    a ``content_ref`` (T5.6+); records without a blob have
    ``content: null`` (the T5.5 common case).

Session identity
================

``session_id`` is the base64url-encoded form of
``"{date}|{actor_type}|{actor_handle}"``. Opaque to clients but
decodeable server-side, which lets the detail endpoint resolve the
underlying records without a side index.

Actor handle
============

Different ingest streams use different fields to identify the same
logical principal:

  - Code Analytics user_actor → ``email_address``
  - Code Analytics api_actor → ``api_key_name``
  - Compliance Activity user_actor → ``email_address`` (matches CA)
  - Compliance Activity api_actor → ``api_key_id`` (DIFFERENT field)

We extract the actor handle with a ``COALESCE`` over the candidate
fields. user_actor matches cleanly across streams; api_actor doesn't
(the api_key_name ↔ api_key_id mismatch is a vendor-side mismatch
between the two APIs). T5.x can introduce a canonical actor mapping
if that asymmetry becomes a real customer concern; for T5.5 the
user_actor path is the common case.

RLS scoping
===========

Every query runs under ``session_scope(tenant_id)`` with the
authenticated user's ``user.tenant_id``. RLS enforces the
tenant-isolation invariant at the DB level — a malicious caller who
guesses or constructs another tenant's ``session_id`` cannot read
its data even with the right opaque token. The 404 response for
cross-tenant lookups is a natural consequence (the query returns
zero rows under the requesting tenant's RLS view).
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import date, datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text

from vargate_telemetry.auth.middleware import AuthenticatedUser, current_user
from vargate_telemetry.db import session_scope


_log = logging.getLogger(__name__)

router = APIRouter()


# ───────────────────────────────────────────────────────────────────────────
# Constants
# ───────────────────────────────────────────────────────────────────────────


# Source APIs that contribute records to Sessions. Admin API usage
# records are bucket-grain (no actor field), so they're excluded.
#
# TM2 Phase E1 adds 'mcp' — every MCP record carries a `subject_user_id`
# and the `metadata.actor.type/user_id/email` triplet (see the persist
# task's metadata builder), so it grains cleanly into the same
# (date, actor) Session shape that the Compliance Activity stream uses.
_SESSION_SOURCE_APIS = (
    "code_analytics",
    "compliance_activities",
    "mcp",
)

# TM2 Phase E1: MCP rows have flat metadata (user_email,
# subject_user_id, no `metadata.actor.*` envelope) because the
# persist task in TM1 wrote them that way. Extend the COALESCE
# chain to read those flat fields too — adding them at the END
# of the priority list means existing actor-shaped streams
# (Compliance Activities, Code Analytics) keep their current
# extraction, and MCP rows that lack the nested envelope fall
# through cleanly.
#
# Per-stream actor_type for MCP records is also flat — we synthesize
# it as 'user_actor' since the MCP identity is always a real
# Ogma-side user (no service-account variant exists yet).


# SQL fragment that extracts the actor's natural identifier from
# `metadata.actor`. COALESCE walks the candidate fields in priority
# order: email (user_actor across both streams), api_key_name (Code
# Analytics api_actor), user_id (Activity Feed user_actor without
# email), api_key_id (Activity Feed api_actor). Falls through to the
# raw actor type string when none of the candidate fields are set so
# groupings still work for novel actor variants. (We dropped the
# earlier ``... || ':unknown'`` suffix because SQLAlchemy's
# ``text()`` parser interprets the literal ``:unknown`` as a bind
# parameter placeholder.)
_ACTOR_KEY_SQL = (
    "COALESCE("
    "  metadata->'actor'->>'email_address',"
    "  metadata->'actor'->>'api_key_name',"
    "  metadata->'actor'->>'user_id',"
    "  metadata->'actor'->>'api_key_id',"
    "  metadata->'actor'->>'type',"
    # TM2: MCP-flat shape — fall through to top-level metadata
    # fields if the nested actor envelope is absent.
    "  metadata->>'user_email',"
    "  metadata->>'subject_user_id'"
    ")"
)


# Same idea for actor_type: MCP rows don't carry one in metadata,
# so synthesize 'user_actor' for them. The SQL coalesces the nested
# field first and falls back when source_api='mcp'.
_ACTOR_TYPE_SQL = (
    "COALESCE("
    "  metadata->'actor'->>'type',"
    "  CASE WHEN source_api = 'mcp' THEN 'user_actor' END"
    ")"
)


# TM4 #3 — "effective surface" for the per-session source distribution.
# MCP records self-report a `surface` (claude_code / claude_web /
# claude_desktop / other); for records logged before the field shipped,
# fall back to the kind=tool_use heuristic, else the generic 'mcp'
# token. Non-mcp sources pass through unchanged. Bare column refs (no
# table alias) — the Sessions aggregation is single-table, so
# `source_api` and `metadata` are unambiguous. MIRRORS the users
# package's EFFECTIVE_SURFACE_SQL (tr.-qualified there for its JOIN
# contexts); KEEP IN SYNC, same duplication posture as _ACTOR_KEY_SQL.
_EFFECTIVE_SURFACE_SQL = (
    "CASE "
    "WHEN source_api = 'mcp' THEN CASE "
    "WHEN NULLIF(metadata->>'surface', '') IS NOT NULL "
    "THEN metadata->>'surface' "
    "WHEN metadata->>'kind' = 'tool_use' THEN 'claude_code' "
    "ELSE 'mcp' END "
    "ELSE source_api END"
)


# ───────────────────────────────────────────────────────────────────────────
# Response shapes — match openapi/ogma-api.yaml
# ───────────────────────────────────────────────────────────────────────────


class SessionActor(BaseModel):
    type: str
    key: str


class SessionSummary(BaseModel):
    # Pydantic 2 can't disambiguate ``date: date`` (field name == type
    # name from datetime). Field is named ``session_date`` internally;
    # JSON serializes as ``"date"`` per the OpenAPI shape via the alias.
    model_config = {"populate_by_name": True}

    session_id: str
    session_date: date = Field(alias="date")
    actor: SessionActor
    source_apis: list[str]
    event_count: int
    # TM2 Phase E1 — per-source breakdown of the session's event count.
    # Keys are source_api values (the same set that appears in
    # `source_apis`); values are how many events came from each.
    # Sums to `event_count`. Frontend uses this to render the
    # source-distribution badge on the Sessions row.
    event_count_by_source: dict[str, int]
    first_seen: datetime
    last_seen: datetime


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]
    next_cursor: Optional[str] = None


class SessionRecord(BaseModel):
    record_id: str
    source_api: str
    record_type: str
    occurred_at: datetime
    external_id: str
    metadata: dict[str, Any]
    content_size_bytes: Optional[int] = None
    content: Optional[str] = None


class SessionDetailResponse(BaseModel):
    # Same date-vs-date name clash as SessionSummary — see comment above.
    model_config = {"populate_by_name": True}

    session_id: str
    session_date: date = Field(alias="date")
    actor: SessionActor
    records: list[SessionRecord]


# ───────────────────────────────────────────────────────────────────────────
# session_id encoding helpers
# ───────────────────────────────────────────────────────────────────────────


_SESSION_ID_SEP = "|"


def _encode_session_id(d: date, actor_type: str, actor_key: str) -> str:
    """Encode a Session's natural key as an opaque, URL-safe string.

    Format: base64url(``"{date}|{actor_type}|{actor_key}"``). The
    separator is a literal pipe — actor_keys can contain ``@``, ``.``,
    ``+``, hyphens, etc., and we don't want to URL-encode every
    response on the way out. The pipe is rare in real actor handles
    but defensively reject it on encode to keep round-trips stable.
    """
    if _SESSION_ID_SEP in actor_type or _SESSION_ID_SEP in actor_key:
        # Pipe inside an actor handle is the only ambiguity case; a
        # vendor-side actor with a literal pipe in the email would
        # corrupt the decode. Not a security boundary — just a
        # round-trip guard.
        actor_type = actor_type.replace(_SESSION_ID_SEP, "_")
        actor_key = actor_key.replace(_SESSION_ID_SEP, "_")
    raw = f"{d.isoformat()}{_SESSION_ID_SEP}{actor_type}{_SESSION_ID_SEP}{actor_key}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def _decode_session_id(session_id: str) -> tuple[date, str, str]:
    """Reverse of ``_encode_session_id``. Raises HTTPException 400 on
    malformed input — opaque does NOT mean "trust the client to send
    a real id," only "the client can't construct one from
    semantically meaningful pieces without decoding ours first."

    Returns ``(date, actor_type, actor_key)``.
    """
    try:
        # Re-pad for urlsafe_b64decode tolerance.
        padded = session_id + "=" * (-len(session_id) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_session_id",
                "message": "session_id is malformed (not valid base64url).",
            },
        ) from exc

    parts = raw.split(_SESSION_ID_SEP)
    if len(parts) != 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_session_id",
                "message": f"session_id decoded but did not have three parts (got {len(parts)}).",
            },
        )
    date_str, actor_type, actor_key = parts
    try:
        d = date.fromisoformat(date_str)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_session_id",
                "message": f"session_id date component is not ISO 8601 (got {date_str!r}).",
            },
        ) from exc
    return d, actor_type, actor_key


# Cursor encoding for the list endpoint. The cursor is the
# ``(last_seen, actor_type, actor_key)`` of the last row in the page —
# enough to resume with a strictly-less-than-or-equal comparator. We
# opaque-wrap it via base64url so clients don't read or construct it.


def _encode_list_cursor(
    last_seen: datetime, actor_type: str, actor_key: str
) -> str:
    payload = json.dumps(
        {
            "ts": last_seen.isoformat(),
            "actor_type": actor_type,
            "actor_key": actor_key,
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(
        payload.encode("utf-8")
    ).rstrip(b"=").decode("ascii")


def _decode_list_cursor(cursor: str) -> tuple[datetime, str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(padded.encode("ascii"))
        )
        return (
            datetime.fromisoformat(payload["ts"]),
            payload["actor_type"],
            payload["actor_key"],
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_cursor",
                "message": "cursor is malformed (re-fetch the first page).",
            },
        ) from exc


# ───────────────────────────────────────────────────────────────────────────
# GET /sessions
# ───────────────────────────────────────────────────────────────────────────


@router.get(
    "/sessions",
    response_model=SessionListResponse,
    operation_id="listSessions",
    tags=["sessions"],
    summary="List Sessions for the authenticated tenant",
)
def list_sessions(
    cursor: Optional[str] = Query(None),
    # T5.5.7 raised the cap from 200 → 1000 so the chart strip can
    # pull enough sessions to render a meaningful trend without a
    # separate /chart endpoint (see vargate-frontend CLAUDE.md).
    limit: int = Query(50, ge=1, le=1000),
    source_api: Optional[str] = Query(None),
    actor_key: Optional[str] = Query(None),
    since: Optional[date] = Query(None),
    until: Optional[date] = Query(None),
    user: AuthenticatedUser = Depends(current_user),
) -> SessionListResponse:
    """Aggregate ``(date, actor_type, actor_key)`` across the eligible
    source APIs. Sort newest-first by ``last_seen`` (max
    ``occurred_at`` in the group), break ties by actor_key for
    cursor stability."""
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "no_tenant_bound",
                "message": "Your session is not bound to a tenant yet.",
            },
        )

    # Validate source_api filter against the eligible set if provided.
    if source_api is not None and source_api not in _SESSION_SOURCE_APIS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_source_api",
                "message": (
                    f"source_api must be one of {_SESSION_SOURCE_APIS!r}; "
                    f"got {source_api!r}."
                ),
            },
        )

    # Decode resume cursor if supplied.
    cursor_ts: Optional[datetime] = None
    cursor_actor_type: Optional[str] = None
    cursor_actor_key: Optional[str] = None
    if cursor is not None:
        cursor_ts, cursor_actor_type, cursor_actor_key = _decode_list_cursor(
            cursor
        )

    # Build the aggregation SQL. The ``DATE(occurred_at AT TIME ZONE 'UTC')``
    # extraction gives the session date in UTC; the ``GROUP BY`` triple is
    # the Session's natural key. ``ARRAY_AGG(DISTINCT ...)`` collects which
    # streams contributed.
    params: dict[str, Any] = {"limit": limit}
    where_clauses = [
        "tenant_id = current_setting('app.tenant_id')",
        f"source_api = ANY(:source_api_filter)",
    ]
    if source_api is not None:
        params["source_api_filter"] = [source_api]
    else:
        params["source_api_filter"] = list(_SESSION_SOURCE_APIS)

    if actor_key is not None:
        where_clauses.append(f"{_ACTOR_KEY_SQL} = :actor_key_filter")
        params["actor_key_filter"] = actor_key
    if since is not None:
        where_clauses.append("occurred_at >= :since_ts")
        params["since_ts"] = datetime.combine(
            since, datetime.min.time()
        )
    if until is not None:
        # Inclusive upper bound on the date; use start-of-day-after.
        from datetime import timedelta as _td

        where_clauses.append("occurred_at < :until_ts")
        params["until_ts"] = datetime.combine(
            until + _td(days=1), datetime.min.time()
        )

    where_sql = " AND ".join(where_clauses)

    # The aggregate query: produce one row per (date, actor_type, actor_key).
    # Cursor filter is applied AFTER aggregation since the cursor's
    # `last_seen` is the MAX(occurred_at) of a group, not a single row.
    #
    # The cursor WHERE clause is built conditionally — passing untyped
    # NULL parameters inside a CASE expression breaks Postgres' type
    # inference (`AmbiguousParameter: could not determine data type of
    # parameter $N`). Building the clause only when cursor is present
    # gives Postgres concrete types every time.
    cursor_clause = ""
    if cursor_ts is not None:
        cursor_clause = (
            "WHERE (last_seen, actor_type, actor_key) "
            "    < (CAST(:cursor_ts AS timestamptz),"
            "       CAST(:cursor_actor_type AS text),"
            "       CAST(:cursor_actor_key AS text))"
        )
        params["cursor_ts"] = cursor_ts
        params["cursor_actor_type"] = cursor_actor_type
        params["cursor_actor_key"] = cursor_actor_key

    # Two-stage aggregation: first GROUP BY session + source_api so
    # we can emit the per-source counts via jsonb_object_agg, then
    # roll those up into one row per session. The cost is one
    # extra CTE pass — but it's the same set of rows, and PG's
    # planner folds the two aggregates into a single HashAggregate.
    sql = f"""
        WITH per_source AS (
            SELECT
                DATE(occurred_at AT TIME ZONE 'UTC') AS session_date,
                {_ACTOR_TYPE_SQL} AS actor_type,
                {_ACTOR_KEY_SQL} AS actor_key,
                ({_EFFECTIVE_SURFACE_SQL}) AS source_api,
                COUNT(*) AS source_count,
                MIN(occurred_at) AS source_first,
                MAX(occurred_at) AS source_last
            FROM telemetry_records
            WHERE {where_sql}
            GROUP BY 1, 2, 3, 4
        ),
        aggregated AS (
            SELECT
                session_date,
                actor_type,
                actor_key,
                ARRAY_AGG(DISTINCT source_api ORDER BY source_api) AS source_apis,
                jsonb_object_agg(source_api, source_count)
                    AS event_count_by_source,
                SUM(source_count)::bigint AS event_count,
                MIN(source_first) AS first_seen,
                MAX(source_last) AS last_seen
            FROM per_source
            GROUP BY 1, 2, 3
        )
        SELECT session_date, actor_type, actor_key, source_apis,
               event_count_by_source, event_count, first_seen, last_seen
        FROM aggregated
        {cursor_clause}
        ORDER BY last_seen DESC, actor_type, actor_key
        LIMIT :limit + 1
    """

    with session_scope(user.tenant_id) as s:
        rows = s.execute(sql_text(sql), params).all()

    # `LIMIT + 1` trick: if we got more rows than `limit`, the last one is
    # the next page's first row — drop it and emit a cursor pointing at the
    # row we DID return (the limit-th row). If we got <= limit rows, this
    # was the last page.
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor: Optional[str] = None
    if has_more and page_rows:
        last_row = page_rows[-1]
        next_cursor = _encode_list_cursor(
            last_row.last_seen,
            last_row.actor_type or "",
            last_row.actor_key or "",
        )

    sessions = [
        SessionSummary(
            session_id=_encode_session_id(
                r.session_date,
                r.actor_type or "",
                r.actor_key or "",
            ),
            session_date=r.session_date,
            actor=SessionActor(
                type=r.actor_type or "",
                key=r.actor_key or "",
            ),
            source_apis=list(r.source_apis),
            event_count=int(r.event_count),
            event_count_by_source={
                k: int(v) for k, v in (r.event_count_by_source or {}).items()
            },
            first_seen=r.first_seen,
            last_seen=r.last_seen,
        )
        for r in page_rows
    ]

    return SessionListResponse(sessions=sessions, next_cursor=next_cursor)


# ───────────────────────────────────────────────────────────────────────────
# GET /sessions/{session_id}
# ───────────────────────────────────────────────────────────────────────────


# Content-decryption injection seam — production wires the real
# `vargate_telemetry.storage.content.retrieve_content`. Tests can
# substitute a stub to avoid the live MinIO + HSM dependency.
_ContentRetriever = Any


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


@router.get(
    "/sessions/{session_id}",
    response_model=SessionDetailResponse,
    operation_id="getSessionDetail",
    tags=["sessions"],
    summary="Get one Session's full record list",
)
def get_session_detail(
    session_id: str = Path(..., min_length=1),
    user: AuthenticatedUser = Depends(current_user),
) -> SessionDetailResponse:
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "no_tenant_bound",
                "message": "Your session is not bound to a tenant yet.",
            },
        )

    session_date, actor_type, actor_key = _decode_session_id(session_id)

    sql = f"""
        SELECT
            id::text AS record_id,
            source_api,
            record_type,
            occurred_at,
            external_id,
            metadata,
            content_ref,
            content_size_bytes
        FROM telemetry_records
        WHERE tenant_id = current_setting('app.tenant_id')
          AND DATE(occurred_at AT TIME ZONE 'UTC') = :session_date
          AND {_ACTOR_TYPE_SQL} = :actor_type
          AND {_ACTOR_KEY_SQL} = :actor_key
          AND source_api = ANY(:source_api_filter)
        ORDER BY occurred_at, chain_seq
    """
    params = {
        "session_date": session_date,
        "actor_type": actor_type,
        "actor_key": actor_key,
        "source_api_filter": list(_SESSION_SOURCE_APIS),
    }

    with session_scope(user.tenant_id) as s:
        rows = s.execute(sql_text(sql), params).all()

    if not rows:
        # Either the session_id is real but for a different tenant
        # (RLS hides it), or it never existed. Both 404 — leaking the
        # distinction would let an attacker probe for cross-tenant
        # session existence.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "session_not_found",
                "message": "No session with that id for your tenant.",
            },
        )

    records: list[SessionRecord] = []
    for r in rows:
        content_plaintext: Optional[str] = None
        if r.content_ref:
            # Decrypt the MinIO blob via the tenant DEK. T5.6 will be
            # the first ingest path that actually populates
            # content_ref; T5.5 still wires the branch so the dashboard
            # works on day one when content lands.
            try:
                blob = _content_retriever(user.tenant_id, r.content_ref)
                content_plaintext = blob.decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover — surface the error
                # IntegrityError / NotFound / network — log + surface
                # as null content with a marker in metadata. The
                # session itself still renders.
                _log.exception(
                    "session_detail: content decrypt failed for %s/%s",
                    user.tenant_id,
                    r.content_ref,
                )
                content_plaintext = None

        records.append(
            SessionRecord(
                record_id=r.record_id,
                source_api=r.source_api,
                record_type=r.record_type,
                occurred_at=r.occurred_at,
                external_id=r.external_id,
                metadata=r.metadata,
                content_size_bytes=r.content_size_bytes,
                content=content_plaintext,
            )
        )

    return SessionDetailResponse(
        session_id=session_id,
        session_date=session_date,
        actor=SessionActor(type=actor_type, key=actor_key),
        records=records,
    )


__all__ = [
    "router",
    "set_content_retriever_for_test",
]
