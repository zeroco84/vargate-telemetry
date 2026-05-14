# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Budgets CRUD API (TM3 Phase B2).

Five endpoints:

- ``GET    /api/budgets``        — list non-deleted budgets
- ``POST   /api/budgets``        — create
- ``GET    /api/budgets/{id}``   — detail with current-period spend
- ``PATCH  /api/budgets/{id}``   — update threshold / recipients / name
- ``DELETE /api/budgets/{id}``   — soft-delete (sets ``deleted_at``)

Plus one alert endpoint added in B-frontend prep:

- ``GET    /api/budget-alerts``        — list alerts
- ``POST   /api/budget-alerts/{id}/acknowledge``

Auth & authorization
====================

Every endpoint requires ``Depends(current_user)``. The
``AuthenticatedUser`` dataclass exposes ``tenant_id`` which gates
RLS — every read or write happens under
``session_scope(user.tenant_id)`` so the database enforces tenant
isolation. A user without a bound tenant gets 400 from every
endpoint (same behaviour as ``/api/usage``).

**Deviation from TM3 §2.2 spec:** the spec calls for "tenant admin
required for write" but the auth layer has no admin/member role
distinction yet — every authenticated user is implicitly a tenant
member. Until the role system lands, write endpoints accept any
authenticated tenant member. RLS prevents cross-tenant interference;
the gap is only intra-tenant. Flagged for TM4.

Soft delete
===========

``DELETE /api/budgets/{id}`` sets ``deleted_at`` and does NOT remove
the row. The list endpoint filters ``WHERE deleted_at IS NULL``; the
detail endpoint also rejects soft-deleted budgets so accidental
revives via the UI aren't a thing. ``budget_alert_events`` rows
remain queryable so historical alerts on a deleted budget don't
disappear (audit-chain principle).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from sqlalchemy import text as sql_text

from vargate_telemetry.auth.middleware import (
    AuthenticatedUser,
    current_user,
)
from vargate_telemetry.budgets import (
    ALERT_THRESHOLDS,
    compute_spend_in_window,
    current_period_window,
)
from vargate_telemetry.db import session_scope

_log = logging.getLogger(__name__)

router = APIRouter()


# ───────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ───────────────────────────────────────────────────────────────────────────


ScopeKind = Literal["api_key", "workspace", "model", "tenant"]
Period = Literal["daily", "weekly", "monthly"]


def _validate_scope_pair(
    scope_kind: ScopeKind, scope_value: Optional[str]
) -> None:
    """Mirror the SQL CHECK constraint at the Pydantic layer.

    Catches bad combinations before they hit the DB so the caller
    gets a 422 with a useful field-pointer rather than the raw
    Postgres constraint violation.
    """
    if scope_kind == "tenant" and scope_value is not None:
        raise ValueError(
            "scope_kind='tenant' must omit scope_value (set to null)"
        )
    if scope_kind != "tenant" and not scope_value:
        raise ValueError(
            f"scope_kind={scope_kind!r} requires a non-empty scope_value"
        )


class BudgetCreate(BaseModel):
    """Request body for ``POST /api/budgets``."""

    name: str = Field(..., min_length=1, max_length=256)
    scope_kind: ScopeKind
    scope_value: Optional[str] = Field(None, max_length=256)
    period: Period
    threshold_usd: Decimal = Field(..., gt=Decimal("0"))
    alert_recipients: list[EmailStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_scope(self) -> "BudgetCreate":
        _validate_scope_pair(self.scope_kind, self.scope_value)
        return self


class BudgetUpdate(BaseModel):
    """Request body for ``PATCH /api/budgets/{id}``.

    All fields optional — only the supplied ones are touched. The
    immutable axes (scope_kind, scope_value, period) are not in the
    update model. If a tenant needs to change those, they delete
    the old budget and create a new one; this avoids confusing
    semantics where an alert period suddenly resets because the
    user shortened the period.
    """

    name: Optional[str] = Field(None, min_length=1, max_length=256)
    threshold_usd: Optional[Decimal] = Field(None, gt=Decimal("0"))
    alert_recipients: Optional[list[EmailStr]] = None


class BudgetOut(BaseModel):
    """Response shape for list + create + patch."""

    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    name: str
    scope_kind: ScopeKind
    scope_value: Optional[str]
    period: Period
    threshold_usd: Decimal
    alert_recipients: list[str]
    created_at: datetime
    updated_at: datetime
    created_by_user_id: Optional[UUID]


class BudgetDetail(BudgetOut):
    """Detail response — adds current-period spend + ratio."""

    current_period_start: date
    current_period_end: date
    current_spend_usd: Decimal
    # ``ratio`` is current_spend / threshold, quantized to 4
    # decimals. Can exceed 1.0 (over budget). Always a Decimal, not
    # a float — financial UI math.
    current_ratio: Decimal
    # The most recent threshold crossed in this period, if any.
    # Null when ratio < 0.70. Useful for the UI's progress-bar
    # color tier (green / yellow / red).
    current_threshold_crossed: Optional[Decimal] = None


class BudgetListResponse(BaseModel):
    rows: list[BudgetOut]


class BudgetAlertEventOut(BaseModel):
    """One row of ``budget_alert_events``."""

    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    budget_id: UUID
    budget_name: str  # Joined from budgets for the UI.
    period_start: date
    threshold_crossed: Decimal
    current_spend_usd: Decimal
    fired_at: datetime
    acknowledged_at: Optional[datetime]
    acknowledged_by_user_id: Optional[UUID]


class BudgetAlertListResponse(BaseModel):
    rows: list[BudgetAlertEventOut]


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _require_tenant(user: AuthenticatedUser) -> str:
    """Reject requests from users not yet bound to a tenant.

    Same shape as ``/api/usage``'s tenant check. Returns the
    tenant_id when present so the caller can pass it straight to
    ``session_scope``.
    """
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "no_tenant_bound",
                "message": "Your session is not bound to a tenant yet.",
            },
        )
    return user.tenant_id


def _row_to_budget_out(row: Any) -> BudgetOut:
    return BudgetOut(
        id=row.id,
        name=row.name,
        scope_kind=row.scope_kind,
        scope_value=row.scope_value,
        period=row.period,
        threshold_usd=row.threshold_usd,
        alert_recipients=list(row.alert_recipients or []),
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by_user_id=row.created_by_user_id,
    )


def _most_recent_threshold_crossed(
    ratio: Decimal,
) -> Optional[Decimal]:
    """Pick the highest threshold ``ratio`` has crossed.

    Returns None when below the lowest threshold. The UI uses this
    to color the progress bar; the evaluator's per-threshold dedup
    handles alert firing independently.
    """
    crossed = [t for t in ALERT_THRESHOLDS if ratio >= t]
    return max(crossed) if crossed else None


# ───────────────────────────────────────────────────────────────────────────
# Routes
# ───────────────────────────────────────────────────────────────────────────


@router.get(
    "/budgets",
    response_model=BudgetListResponse,
    operation_id="listBudgets",
    tags=["budgets"],
    summary="List the authenticated tenant's live (non-deleted) budgets",
)
def list_budgets(
    user: AuthenticatedUser = Depends(current_user),
) -> BudgetListResponse:
    tenant_id = _require_tenant(user)

    with session_scope(tenant_id) as s:
        result = s.execute(
            sql_text(
                """
                SELECT id, name, scope_kind, scope_value, period,
                       threshold_usd, alert_recipients, created_at,
                       updated_at, created_by_user_id
                FROM budgets
                WHERE deleted_at IS NULL
                ORDER BY created_at DESC
                """
            )
        )
        rows = [_row_to_budget_out(r) for r in result]

    return BudgetListResponse(rows=rows)


@router.post(
    "/budgets",
    response_model=BudgetOut,
    operation_id="createBudget",
    tags=["budgets"],
    status_code=status.HTTP_201_CREATED,
    summary="Create a new budget for the authenticated tenant",
)
def create_budget(
    body: BudgetCreate,
    user: AuthenticatedUser = Depends(current_user),
) -> BudgetOut:
    tenant_id = _require_tenant(user)

    # `created_by_user_id` is the JWT subject — we trust the JWT
    # claim because `current_user` already validated it.
    try:
        creator_uuid = UUID(user.user_id)
    except ValueError:
        creator_uuid = None  # pre-UUID user_ids in old fixtures

    with session_scope(tenant_id) as s:
        result = s.execute(
            sql_text(
                """
                INSERT INTO budgets (
                    tenant_id, name, scope_kind, scope_value,
                    period, threshold_usd, alert_recipients,
                    created_by_user_id
                )
                VALUES (
                    :tenant_id, :name, :scope_kind, :scope_value,
                    :period, :threshold_usd, :alert_recipients,
                    :created_by_user_id
                )
                RETURNING id, name, scope_kind, scope_value, period,
                          threshold_usd, alert_recipients,
                          created_at, updated_at, created_by_user_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "name": body.name,
                "scope_kind": body.scope_kind,
                "scope_value": body.scope_value,
                "period": body.period,
                "threshold_usd": body.threshold_usd,
                # EmailStr serializes to plain str — keep the wire
                # representation a flat array of strings.
                "alert_recipients": [str(e) for e in body.alert_recipients],
                "created_by_user_id": creator_uuid,
            },
        ).one()

    return _row_to_budget_out(result)


@router.get(
    "/budgets/{budget_id}",
    response_model=BudgetDetail,
    operation_id="getBudgetDetail",
    tags=["budgets"],
    summary="Detail view with current-period spend + ratio",
)
def get_budget_detail(
    budget_id: UUID = Path(...),
    user: AuthenticatedUser = Depends(current_user),
) -> BudgetDetail:
    tenant_id = _require_tenant(user)

    with session_scope(tenant_id) as s:
        result = s.execute(
            sql_text(
                """
                SELECT id, name, scope_kind, scope_value, period,
                       threshold_usd, alert_recipients, created_at,
                       updated_at, created_by_user_id
                FROM budgets
                WHERE id = :id
                  AND deleted_at IS NULL
                """
            ),
            {"id": str(budget_id)},
        ).first()
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "budget_not_found",
                    "message": f"No live budget with id {budget_id}.",
                },
            )

        window = current_period_window(result.period)
        spend = compute_spend_in_window(
            s,
            start=window.start,
            end=window.end,
            scope_kind=result.scope_kind,
            scope_value=result.scope_value,
        )

    threshold: Decimal = result.threshold_usd
    if threshold > 0:
        ratio = (spend / threshold).quantize(Decimal("0.0001"))
    else:  # pragma: no cover — guarded by ck_budgets_threshold_positive
        ratio = Decimal("0.0000")

    base = _row_to_budget_out(result)
    return BudgetDetail(
        **base.model_dump(),
        current_period_start=window.start.date(),
        current_period_end=window.end.date(),
        current_spend_usd=spend,
        current_ratio=ratio,
        current_threshold_crossed=_most_recent_threshold_crossed(ratio),
    )


@router.patch(
    "/budgets/{budget_id}",
    response_model=BudgetOut,
    operation_id="updateBudget",
    tags=["budgets"],
    summary="Update mutable fields on a budget",
)
def update_budget(
    body: BudgetUpdate,
    budget_id: UUID = Path(...),
    user: AuthenticatedUser = Depends(current_user),
) -> BudgetOut:
    tenant_id = _require_tenant(user)

    set_clauses: list[str] = []
    params: dict[str, Any] = {"id": str(budget_id)}
    if body.name is not None:
        set_clauses.append("name = :name")
        params["name"] = body.name
    if body.threshold_usd is not None:
        set_clauses.append("threshold_usd = :threshold_usd")
        params["threshold_usd"] = body.threshold_usd
    if body.alert_recipients is not None:
        set_clauses.append("alert_recipients = :alert_recipients")
        params["alert_recipients"] = [str(e) for e in body.alert_recipients]

    if not set_clauses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "no_fields_to_update",
                "message": (
                    "PATCH body must include at least one of "
                    "name / threshold_usd / alert_recipients."
                ),
            },
        )

    set_clauses.append("updated_at = now()")
    update_sql = sql_text(
        f"""
        UPDATE budgets
        SET {", ".join(set_clauses)}
        WHERE id = :id
          AND deleted_at IS NULL
        RETURNING id, name, scope_kind, scope_value, period,
                  threshold_usd, alert_recipients, created_at,
                  updated_at, created_by_user_id
        """
    )

    with session_scope(tenant_id) as s:
        result = s.execute(update_sql, params).first()
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "budget_not_found",
                    "message": f"No live budget with id {budget_id}.",
                },
            )

    return _row_to_budget_out(result)


@router.delete(
    "/budgets/{budget_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteBudget",
    tags=["budgets"],
    summary="Soft-delete a budget (sets deleted_at; does not remove the row)",
)
def delete_budget(
    budget_id: UUID = Path(...),
    user: AuthenticatedUser = Depends(current_user),
) -> None:
    tenant_id = _require_tenant(user)
    with session_scope(tenant_id) as s:
        result = s.execute(
            sql_text(
                """
                UPDATE budgets
                SET deleted_at = now()
                WHERE id = :id
                  AND deleted_at IS NULL
                RETURNING id
                """
            ),
            {"id": str(budget_id)},
        ).first()
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "budget_not_found",
                    "message": f"No live budget with id {budget_id}.",
                },
            )


# ───────────────────────────────────────────────────────────────────────────
# Budget alerts (frontend B6)
# ───────────────────────────────────────────────────────────────────────────


@router.get(
    "/budget-alerts",
    response_model=BudgetAlertListResponse,
    operation_id="listBudgetAlerts",
    tags=["budgets"],
    summary="List alert events fired against this tenant's budgets",
)
def list_budget_alerts(
    only_unack: bool = Query(False, alias="unack"),
    user: AuthenticatedUser = Depends(current_user),
) -> BudgetAlertListResponse:
    tenant_id = _require_tenant(user)

    where = "WHERE bae.tenant_id = current_setting('app.tenant_id')"
    if only_unack:
        where += " AND bae.acknowledged_at IS NULL"

    with session_scope(tenant_id) as s:
        result = s.execute(
            sql_text(
                f"""
                SELECT bae.id, bae.budget_id, b.name AS budget_name,
                       bae.period_start, bae.threshold_crossed,
                       bae.current_spend_usd, bae.fired_at,
                       bae.acknowledged_at, bae.acknowledged_by_user_id
                FROM budget_alert_events bae
                JOIN budgets b ON b.id = bae.budget_id
                {where}
                ORDER BY bae.fired_at DESC
                LIMIT 200
                """
            )
        )
        rows = [
            BudgetAlertEventOut(
                id=r.id,
                budget_id=r.budget_id,
                budget_name=r.budget_name,
                period_start=r.period_start,
                threshold_crossed=r.threshold_crossed,
                current_spend_usd=r.current_spend_usd,
                fired_at=r.fired_at,
                acknowledged_at=r.acknowledged_at,
                acknowledged_by_user_id=r.acknowledged_by_user_id,
            )
            for r in result
        ]

    return BudgetAlertListResponse(rows=rows)


@router.post(
    "/budget-alerts/{alert_id}/acknowledge",
    response_model=BudgetAlertEventOut,
    operation_id="acknowledgeBudgetAlert",
    tags=["budgets"],
    summary="Mark a fired alert as acknowledged by the current user",
)
def acknowledge_budget_alert(
    alert_id: UUID = Path(...),
    user: AuthenticatedUser = Depends(current_user),
) -> BudgetAlertEventOut:
    tenant_id = _require_tenant(user)
    try:
        ack_user = UUID(user.user_id)
    except ValueError:
        ack_user = None

    with session_scope(tenant_id) as s:
        result = s.execute(
            sql_text(
                """
                UPDATE budget_alert_events
                SET acknowledged_at = now(),
                    acknowledged_by_user_id = :ack_user
                WHERE id = :id
                  AND acknowledged_at IS NULL
                RETURNING id, budget_id, period_start, threshold_crossed,
                          current_spend_usd, fired_at,
                          acknowledged_at, acknowledged_by_user_id
                """
            ),
            {"id": str(alert_id), "ack_user": ack_user},
        ).first()
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "alert_not_found_or_already_acked",
                    "message": (
                        "No unacknowledged alert with that id. It may "
                        "have already been acknowledged, or it may not "
                        "belong to your tenant."
                    ),
                },
            )
        # Need the budget name for the response shape — separate
        # SELECT keeps the UPDATE's RETURNING clean.
        name_row = s.execute(
            sql_text(
                "SELECT name FROM budgets WHERE id = :id"
            ),
            {"id": str(result.budget_id)},
        ).first()
        budget_name = name_row.name if name_row else ""

    return BudgetAlertEventOut(
        id=result.id,
        budget_id=result.budget_id,
        budget_name=budget_name,
        period_start=result.period_start,
        threshold_crossed=result.threshold_crossed,
        current_spend_usd=result.current_spend_usd,
        fired_at=result.fired_at,
        acknowledged_at=result.acknowledged_at,
        acknowledged_by_user_id=result.acknowledged_by_user_id,
    )


__all__ = ["router"]
