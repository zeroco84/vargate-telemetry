# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Cross-surface users API (TM3 Phase C2).

The unique-to-Ogma analytic: one person, every surface. Anthropic
shows API keys, Claude Code actors, and Claude chat users as three
disconnected dashboards; this endpoint unifies them via the
``user_aliases`` stitching (Phase C1).

Endpoints:
  - GET  /api/users            — roster + per-user rollup + unmapped
  - GET  /api/users/{id}        — one user, cross-source detail
  - POST /api/users/{id}/aliases — admin manually maps an alias

Spend attribution
=================

Admin API usage is key-level (no actor) so it CANNOT attribute to a
person. Code Analytics carries activity metrics but no priceable
token shape. **MCP records are the only per-user priceable source**
— they carry ``input_tokens_estimate`` / ``output_tokens_estimate``
+ ``model``. So per-user spend is MCP-derived and ESTIMATE-based;
where a user has no MCP activity the spend is ``$0.00`` (not faked).
This is surfaced honestly in the UI copy.

Lazy reconcile
=============

``GET /api/users`` reconciles aliases on read (best-effort, wrapped
so a failure doesn't break the list) so a freshly-onboarded tenant
sees stitched users on first load. The 15-minute beat task is the
steady-state path. Both call the same idempotent helper.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text as sql_text

from vargate_telemetry.auth.middleware import AuthenticatedUser, current_user
from vargate_telemetry.auth.roles import (
    ROLE_ADMIN,
    VALID_ROLES,
    count_admins,
    require_admin,
)
from vargate_telemetry.db import session_scope
from vargate_telemetry.pricing import compute_cost_usd
from vargate_telemetry.users import (
    ACTOR_KEY_SQL,
    EFFECTIVE_SURFACE_SQL,
    SESSION_SOURCE_APIS,
    reconcile_aliases_for_tenant,
)

_log = logging.getLogger(__name__)

router = APIRouter()

# Heatmap / spend-trend window. 90 days is plenty for the demo and
# keeps the per-day grid a sane width (~13 weeks). TM4 can make this
# configurable if a customer asks.
_DETAIL_WINDOW_DAYS = 90
_ROLLUP_WINDOW_DAYS = 7
_RECENT_LIMIT = 25


# ───────────────────────────────────────────────────────────────────────────
# Response shapes
# ───────────────────────────────────────────────────────────────────────────


class UserSurfaceStat(BaseModel):
    user_id: str
    email: str
    # TM4: 'admin' | 'member' — drives the role chip + promote/demote
    # control in the roster.
    role: str
    surfaces: list[str]
    events_7d: int
    # Decimal string; None when the user has no priceable (MCP)
    # activity in the window — rendered as "—" in the UI, never $0
    # faked into a real figure.
    spend_7d_usd: Optional[str] = None
    last_active: Optional[datetime] = None


class UnmappedAlias(BaseModel):
    source_api: str
    source_identifier: str
    event_count: int
    last_seen: Optional[datetime] = None


class UserListResponse(BaseModel):
    users: list[UserSurfaceStat]
    unmapped: list[UnmappedAlias]


class HeatmapCell(BaseModel):
    day: date
    source_api: str
    count: int


class SpendTrendPoint(BaseModel):
    day: date
    source_api: str
    spend_usd: str


class UserAliasOut(BaseModel):
    source_api: str
    source_identifier: str
    auto_matched: bool


class UserRecentRecord(BaseModel):
    record_id: str
    source_api: str
    occurred_at: datetime
    kind: Optional[str] = None
    summary: Optional[str] = None


class TopicCount(BaseModel):
    """One taxonomy topic + how many of the user's interactions hit it."""

    topic: str
    count: int


class UserDetailResponse(BaseModel):
    user_id: str
    email: str
    joined_at: datetime
    surfaces: list[str]
    aliases: list[UserAliasOut]
    heatmap: list[HeatmapCell]
    spend_trend: list[SpendTrendPoint]
    recent: list[UserRecentRecord]
    # TM4 Track D — topics inferred from this user's MCP summaries.
    # top_topics is ranked desc; topics_classified / topics_total power
    # the honest "N of M classified" framing (classification is async +
    # best-effort, so total >= classified).
    top_topics: list[TopicCount]
    topics_classified: int
    topics_total: int


class AliasMapRequest(BaseModel):
    source_api: str = Field(..., min_length=1, max_length=32)
    source_identifier: str = Field(..., min_length=1, max_length=320)


class RoleUpdateRequest(BaseModel):
    # Validated against VALID_ROLES in the handler so the error envelope
    # matches our {code, message} shape rather than FastAPI's default.
    role: str = Field(..., min_length=1, max_length=16)


class RoleUpdateResponse(BaseModel):
    user_id: str
    role: str


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


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


def _price_mcp_row(row: Any) -> Optional[Decimal]:
    """Cost for one MCP record from its token estimates. None when
    the model is unknown / unpriceable (never faked)."""
    model = row.model
    if not model:
        return None
    return compute_cost_usd(
        model=model,
        input_tokens=int(row.input_tokens or 0),
        output_tokens=int(row.output_tokens or 0),
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=row.occurred_at,
    )


# ───────────────────────────────────────────────────────────────────────────
# GET /api/users
# ───────────────────────────────────────────────────────────────────────────


@router.get(
    "/users",
    response_model=UserListResponse,
    operation_id="listUsers",
    tags=["users"],
    summary="Cross-surface user roster + per-user rollup + unmapped activity",
)
def list_users(
    user: AuthenticatedUser = Depends(current_user),
) -> UserListResponse:
    tenant_id = _require_tenant(user)
    since_7d = datetime.now(tz=timezone.utc) - timedelta(
        days=_ROLLUP_WINDOW_DAYS
    )

    with session_scope(tenant_id) as s:
        # Lazy reconcile — best-effort. A reconcile failure must not
        # break the read (the list still renders from existing
        # aliases). Activation-readiness: fresh tenant sees stitched
        # users on first load.
        try:
            reconcile_aliases_for_tenant(s, tenant_id)
        except Exception:  # noqa: BLE001
            _log.exception(
                "list_users: lazy reconcile failed for %s; "
                "serving from existing aliases",
                tenant_id,
            )

        # Per-mapped-user rollup: surfaces, 7d events, last active.
        rollup = s.execute(
            sql_text(
                f"""
                WITH actor_events AS (
                    SELECT
                        tr.source_api,
                        ({EFFECTIVE_SURFACE_SQL}) AS surface,
                        {ACTOR_KEY_SQL} AS identifier,
                        tr.occurred_at
                    FROM telemetry_records tr
                    WHERE tr.tenant_id = current_setting('app.tenant_id')
                      AND tr.source_api = ANY(:source_apis)
                      AND {ACTOR_KEY_SQL} IS NOT NULL
                )
                SELECT
                    u.id::text AS user_id,
                    u.email,
                    u.role AS role,
                    array_agg(DISTINCT ae.surface) AS surfaces,
                    count(*) FILTER (
                        WHERE ae.occurred_at >= :since_7d
                    ) AS events_7d,
                    max(ae.occurred_at) AS last_active
                FROM user_aliases ua
                JOIN users u ON u.id = ua.user_id
                JOIN actor_events ae
                   ON ae.source_api = ua.source_api
                  AND ae.identifier = ua.source_identifier
                WHERE ua.user_id IS NOT NULL
                GROUP BY u.id, u.email, u.role
                ORDER BY last_active DESC NULLS LAST
                """
            ),
            {
                "source_apis": list(SESSION_SOURCE_APIS),
                "since_7d": since_7d,
            },
        ).all()

        # Per-user 7d spend from MCP priceable records (the only
        # per-user priceable source). One query, priced in Python.
        spend_rows = s.execute(
            sql_text(
                f"""
                SELECT
                    ua.user_id::text AS user_id,
                    tr.metadata->>'model' AS model,
                    COALESCE(
                        (tr.metadata->>'input_tokens_estimate')::bigint, 0
                    ) AS input_tokens,
                    COALESCE(
                        (tr.metadata->>'output_tokens_estimate')::bigint, 0
                    ) AS output_tokens,
                    tr.occurred_at
                FROM telemetry_records tr
                JOIN user_aliases ua
                   ON ua.source_api = tr.source_api
                  AND ua.source_identifier = {ACTOR_KEY_SQL}
                WHERE tr.tenant_id = current_setting('app.tenant_id')
                  AND tr.source_api = 'mcp'
                  AND ua.user_id IS NOT NULL
                  AND tr.occurred_at >= :since_7d
                """
            ),
            {"since_7d": since_7d},
        ).all()

        spend_by_user: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        has_priceable: set[str] = set()
        for row in spend_rows:
            cost = _price_mcp_row(row)
            if cost is None:
                continue
            spend_by_user[row.user_id] += cost
            has_priceable.add(row.user_id)

        # Unmapped activity: aliases with no user, + their event
        # counts so the admin can prioritize which to resolve.
        unmapped_rows = s.execute(
            sql_text(
                f"""
                WITH actor_events AS (
                    SELECT
                        tr.source_api,
                        ({EFFECTIVE_SURFACE_SQL}) AS surface,
                        {ACTOR_KEY_SQL} AS identifier,
                        tr.occurred_at
                    FROM telemetry_records tr
                    WHERE tr.tenant_id = current_setting('app.tenant_id')
                      AND tr.source_api = ANY(:source_apis)
                      AND {ACTOR_KEY_SQL} IS NOT NULL
                )
                SELECT
                    ua.source_api,
                    ua.source_identifier,
                    count(ae.*) AS event_count,
                    max(ae.occurred_at) AS last_seen
                FROM user_aliases ua
                LEFT JOIN actor_events ae
                   ON ae.source_api = ua.source_api
                  AND ae.identifier = ua.source_identifier
                WHERE ua.user_id IS NULL
                GROUP BY ua.source_api, ua.source_identifier
                ORDER BY count(ae.*) DESC
                """
            ),
            {"source_apis": list(SESSION_SOURCE_APIS)},
        ).all()

    users = [
        UserSurfaceStat(
            user_id=r.user_id,
            email=r.email,
            role=r.role,
            surfaces=sorted(r.surfaces or []),
            events_7d=int(r.events_7d or 0),
            spend_7d_usd=(
                str(spend_by_user[r.user_id].quantize(Decimal("0.01")))
                if r.user_id in has_priceable
                else None
            ),
            last_active=r.last_active,
        )
        for r in rollup
    ]
    unmapped = [
        UnmappedAlias(
            source_api=r.source_api,
            source_identifier=r.source_identifier,
            event_count=int(r.event_count or 0),
            last_seen=r.last_seen,
        )
        for r in unmapped_rows
    ]
    return UserListResponse(users=users, unmapped=unmapped)


# ───────────────────────────────────────────────────────────────────────────
# GET /api/users/{id}
# ───────────────────────────────────────────────────────────────────────────


@router.get(
    "/users/{user_id}",
    response_model=UserDetailResponse,
    operation_id="getUserDetail",
    tags=["users"],
    summary="One user's activity stitched across every surface",
)
def get_user_detail(
    user_id: UUID = Path(...),
    user: AuthenticatedUser = Depends(current_user),
) -> UserDetailResponse:
    tenant_id = _require_tenant(user)
    window_start = datetime.now(tz=timezone.utc) - timedelta(
        days=_DETAIL_WINDOW_DAYS
    )

    with session_scope(tenant_id) as s:
        urow = s.execute(
            sql_text(
                """
                SELECT id::text AS id, email, created_at
                FROM users
                WHERE id = :id AND tenant_id = :tenant_id
                """
            ),
            {"id": str(user_id), "tenant_id": tenant_id},
        ).first()
        if urow is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "user_not_found",
                    "message": f"No user {user_id} in this tenant.",
                },
            )

        alias_rows = s.execute(
            sql_text(
                """
                SELECT source_api, source_identifier, auto_matched
                FROM user_aliases
                WHERE user_id = :id
                ORDER BY source_api, source_identifier
                """
            ),
            {"id": str(user_id)},
        ).all()
        aliases = [
            UserAliasOut(
                source_api=a.source_api,
                source_identifier=a.source_identifier,
                auto_matched=a.auto_matched,
            )
            for a in alias_rows
        ]

        if not aliases:
            # Mapped user with no aliases yet — return an empty shell
            # rather than 404. The user exists; they just have no
            # attributed activity.
            return UserDetailResponse(
                user_id=urow.id,
                email=urow.email,
                joined_at=urow.created_at,
                surfaces=[],
                aliases=[],
                heatmap=[],
                spend_trend=[],
                recent=[],
                top_topics=[],
                topics_classified=0,
                topics_total=0,
            )

        # Heatmap: per (day, source) event count over the window.
        heatmap_rows = s.execute(
            sql_text(
                f"""
                WITH actor_events AS (
                    SELECT
                        tr.source_api,
                        ({EFFECTIVE_SURFACE_SQL}) AS surface,
                        {ACTOR_KEY_SQL} AS identifier,
                        tr.occurred_at
                    FROM telemetry_records tr
                    WHERE tr.tenant_id = current_setting('app.tenant_id')
                      AND tr.source_api = ANY(:source_apis)
                      AND tr.occurred_at >= :window_start
                      AND {ACTOR_KEY_SQL} IS NOT NULL
                )
                SELECT
                    DATE(ae.occurred_at AT TIME ZONE 'UTC') AS day,
                    ae.surface AS source_api,
                    count(*) AS count
                FROM actor_events ae
                JOIN user_aliases ua
                   ON ua.source_api = ae.source_api
                  AND ua.source_identifier = ae.identifier
                WHERE ua.user_id = :id
                GROUP BY day, ae.surface
                ORDER BY day
                """
            ),
            {
                "source_apis": list(SESSION_SOURCE_APIS),
                "window_start": window_start,
                "id": str(user_id),
            },
        ).all()
        heatmap = [
            HeatmapCell(
                day=h.day, source_api=h.source_api, count=int(h.count)
            )
            for h in heatmap_rows
        ]
        # TM4 #3 — header surfaces reflect the EFFECTIVE surface
        # ("Claude Code" vs "Claude (chat)") seen over the heatmap
        # window, derived from the same rows the grid renders rather
        # than the raw alias source_api (which is always 'mcp').
        surfaces = sorted({c.source_api for c in heatmap})

        # Spend trend: per-day MCP cost (priceable source). Priced in
        # Python; grouped by day.
        spend_src = s.execute(
            sql_text(
                f"""
                SELECT
                    DATE(tr.occurred_at AT TIME ZONE 'UTC') AS day,
                    tr.metadata->>'model' AS model,
                    COALESCE(
                        (tr.metadata->>'input_tokens_estimate')::bigint, 0
                    ) AS input_tokens,
                    COALESCE(
                        (tr.metadata->>'output_tokens_estimate')::bigint, 0
                    ) AS output_tokens,
                    tr.occurred_at
                FROM telemetry_records tr
                JOIN user_aliases ua
                   ON ua.source_api = tr.source_api
                  AND ua.source_identifier = {ACTOR_KEY_SQL}
                WHERE tr.tenant_id = current_setting('app.tenant_id')
                  AND tr.source_api = 'mcp'
                  AND ua.user_id = :id
                  AND tr.occurred_at >= :window_start
                """
            ),
            {"window_start": window_start, "id": str(user_id)},
        ).all()
        spend_by_day: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
        for row in spend_src:
            cost = _price_mcp_row(row)
            if cost is None:
                continue
            spend_by_day[row.day] += cost
        spend_trend = [
            SpendTrendPoint(
                day=d,
                source_api="mcp",
                spend_usd=str(spend_by_day[d].quantize(Decimal("0.01"))),
            )
            for d in sorted(spend_by_day)
        ]

        # Recent activity, cross-source, newest first.
        recent_rows = s.execute(
            sql_text(
                f"""
                SELECT
                    tr.id::text AS record_id,
                    ({EFFECTIVE_SURFACE_SQL}) AS source_api,
                    tr.occurred_at,
                    tr.metadata->>'kind' AS kind,
                    tr.metadata->>'summary' AS summary
                FROM telemetry_records tr
                JOIN user_aliases ua
                   ON ua.source_api = tr.source_api
                  AND ua.source_identifier = {ACTOR_KEY_SQL}
                WHERE tr.tenant_id = current_setting('app.tenant_id')
                  AND ua.user_id = :id
                ORDER BY tr.occurred_at DESC
                LIMIT :limit
                """
            ),
            {"id": str(user_id), "limit": _RECENT_LIMIT},
        ).all()
        recent = [
            UserRecentRecord(
                record_id=r.record_id,
                source_api=r.source_api,
                occurred_at=r.occurred_at,
                kind=r.kind,
                summary=r.summary,
            )
            for r in recent_rows
        ]

        # TM4 Track D — top topics inferred from this user's MCP
        # summaries (only MCP records carry topic classifications). The
        # join walks interaction_topics -> the record -> the user's
        # alias. Ranked desc; ties broken by topic name for stability.
        topic_rows = s.execute(
            sql_text(
                f"""
                SELECT it.topic AS topic, count(*) AS n
                FROM interaction_topics it
                JOIN telemetry_records tr ON tr.id = it.record_id
                JOIN user_aliases ua
                   ON ua.source_api = tr.source_api
                  AND ua.source_identifier = {ACTOR_KEY_SQL}
                WHERE it.tenant_id = current_setting('app.tenant_id')
                  AND tr.source_api = 'mcp'
                  AND ua.user_id = :id
                GROUP BY it.topic
                ORDER BY n DESC, it.topic
                """
            ),
            {"id": str(user_id)},
        ).all()
        top_topics = [
            TopicCount(topic=r.topic, count=int(r.n)) for r in topic_rows
        ]
        topics_classified = sum(int(r.n) for r in topic_rows)
        # The classifiable universe: the user's MCP records that carry a
        # summary. total >= classified (classification is async + capped
        # per tick), so the UI can honestly show "N of M classified".
        topics_total = (
            s.execute(
                sql_text(
                    f"""
                    SELECT count(*) AS n
                    FROM telemetry_records tr
                    JOIN user_aliases ua
                       ON ua.source_api = tr.source_api
                      AND ua.source_identifier = {ACTOR_KEY_SQL}
                    WHERE tr.tenant_id = current_setting('app.tenant_id')
                      AND tr.source_api = 'mcp'
                      AND ua.user_id = :id
                      AND COALESCE(tr.metadata->>'summary', '') <> ''
                    """
                ),
                {"id": str(user_id)},
            ).scalar()
            or 0
        )

    return UserDetailResponse(
        user_id=urow.id,
        email=urow.email,
        joined_at=urow.created_at,
        surfaces=surfaces,
        aliases=aliases,
        heatmap=heatmap,
        spend_trend=spend_trend,
        recent=recent,
        top_topics=top_topics,
        topics_classified=topics_classified,
        topics_total=int(topics_total),
    )


# ───────────────────────────────────────────────────────────────────────────
# POST /api/users/{id}/aliases — manual mapping
# ───────────────────────────────────────────────────────────────────────────


@router.post(
    "/users/{user_id}/aliases",
    response_model=UserAliasOut,
    operation_id="mapUserAlias",
    tags=["users"],
    status_code=status.HTTP_201_CREATED,
    summary="Manually map an (source_api, identifier) alias to this user",
)
def map_user_alias(
    body: AliasMapRequest,
    user_id: UUID = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
) -> UserAliasOut:
    tenant_id = _require_tenant(user)

    with session_scope(tenant_id) as s:
        # The user must exist in this tenant.
        urow = s.execute(
            sql_text(
                "SELECT id FROM users WHERE id = :id AND tenant_id = :t"
            ),
            {"id": str(user_id), "t": tenant_id},
        ).first()
        if urow is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "user_not_found",
                    "message": f"No user {user_id} in this tenant.",
                },
            )

        # Upsert the alias → this user, auto_matched=false (manual).
        # The false flag protects it from future auto-match overwrites
        # (the reconciler's UPSERT only touches auto_matched=true rows).
        row = s.execute(
            sql_text(
                """
                INSERT INTO user_aliases (
                    tenant_id, user_id, source_api,
                    source_identifier, auto_matched
                )
                VALUES (
                    current_setting('app.tenant_id'),
                    :user_id, :source_api, :identifier, false
                )
                ON CONFLICT (tenant_id, source_api, source_identifier)
                DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    auto_matched = false,
                    updated_at = now()
                RETURNING source_api, source_identifier, auto_matched
                """
            ),
            {
                "user_id": str(user_id),
                "source_api": body.source_api,
                "identifier": body.source_identifier,
            },
        ).one()

    return UserAliasOut(
        source_api=row.source_api,
        source_identifier=row.source_identifier,
        auto_matched=row.auto_matched,
    )


# ───────────────────────────────────────────────────────────────────────────
# POST /api/users/{id}/role — admin-only role change (TM4)
# ───────────────────────────────────────────────────────────────────────────


@router.post(
    "/users/{user_id}/role",
    response_model=RoleUpdateResponse,
    operation_id="setUserRole",
    tags=["users"],
    summary="Set a tenant member's role ('admin' | 'member') — admin only",
)
def set_user_role(
    body: RoleUpdateRequest,
    user_id: UUID = Path(...),
    admin: AuthenticatedUser = Depends(require_admin),
) -> RoleUpdateResponse:
    new_role = body.role.strip().lower()
    if new_role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_role",
                "message": "role must be 'admin' or 'member'.",
            },
        )

    tenant_id = admin.tenant_id  # require_admin guarantees a bound tenant
    target_id = str(user_id)

    with session_scope(tenant_id) as s:
        target = s.execute(
            sql_text(
                "SELECT role FROM users WHERE id = :id AND tenant_id = :t"
            ),
            {"id": target_id, "t": tenant_id},
        ).first()
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "user_not_found",
                    "message": f"No user {user_id} in this tenant.",
                },
            )

        # Last-admin guard: refuse to remove the tenant's final admin —
        # that would lock everyone out of budget + identity writes.
        # Self-demotion is allowed as long as another admin remains.
        if target.role == ROLE_ADMIN and new_role != ROLE_ADMIN:
            if count_admins(s, tenant_id) <= 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "last_admin",
                        "message": (
                            "Can't remove the only admin — promote "
                            "another user to admin first."
                        ),
                    },
                )

        s.execute(
            sql_text(
                "UPDATE users SET role = :r "
                "WHERE id = :id AND tenant_id = :t"
            ),
            {"r": new_role, "id": target_id, "t": tenant_id},
        )

    return RoleUpdateResponse(user_id=target_id, role=new_role)


__all__ = ["router"]
