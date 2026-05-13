# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Usage API (T5.5.5) — Admin API daily-aggregate dashboard view.

A **UsageRow** is one breakdown group inside one Admin API daily
bucket: ``(date, workspace_id, model)`` from
``metadata.results[idx]`` on a ``record_type='usage'``,
``source_api='admin'`` record. Each ``telemetry_records`` row is a
single day's pull from
``/v1/organizations/usage_report/messages`` and contains zero or
more result groups in ``metadata.results``; this endpoint flattens
the array into a row per group.

T5.5.5 invariant
================
This endpoint reads ONLY ``record_type='usage'`` AND
``source_api='admin'`` records. Sessions
(``code_analytics``/``compliance_activities``) live in ``/sessions``
and have an actor dimension; Admin API usage is bucket-grain (no
actor) and lives here. The two surfaces are complementary, not
competing.

Why the response is not pre-aggregated to per-day totals
========================================================
For tenants whose connector requests breakdowns via
``group_by=[workspace_id, model]`` (a future improvement; T5.5.5
ships with the unbreakdown'd connector), one day can carry many
result groups. The endpoint returns one row per group so the UI can
render workspace × model granularity when it shows up. For today's
unbreakdown'd connector, every day collapses to one row with
``workspace_id = null`` and ``model = null``.

Totals row
==========
``totals`` is computed across the **full filtered set**, not just
the current page. The frontend needs the total-spend figure
regardless of which page the user is on; computing it client-side
from page rows would silently undercount on multi-page results.

RLS scoping
===========
Same pattern as ``/sessions``: every query runs under
``session_scope(tenant_id)`` with the caller's tenant binding.

Out of scope for T5.5.5
=======================
- ``estimated_cost_usd``: requires per-model pricing data, and
  today's connector doesn't request ``group_by=model`` so
  ``model`` is always null for the founder's tenant. The spec
  permits adapting to what the DB supports — we drop the field
  for v1 and surface the gap in the follow-ups list.
- Workspace name resolution: the ``metadata.results[].workspace_id``
  is the vendor opaque id; rendering a human-friendly name needs
  a workspaces side table populated from the Admin API's
  ``/workspaces`` endpoint. Flagged for T5.x.
- CSV export.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text

from vargate_telemetry.auth.middleware import AuthenticatedUser, current_user
from vargate_telemetry.db import session_scope


_log = logging.getLogger(__name__)

router = APIRouter()


# ───────────────────────────────────────────────────────────────────────────
# Response shapes
# ───────────────────────────────────────────────────────────────────────────


class UsageRow(BaseModel):
    """One (date, workspace_id, model) breakdown row.

    All token counters are ``int`` because the Admin API never
    returns fractional tokens; ``None`` for the breakdown keys means
    the connector didn't request ``group_by`` for that dimension —
    not "all workspaces" semantically.
    """

    model_config = {"populate_by_name": True}

    row_date: date = Field(alias="date")
    workspace_id: Optional[str] = None
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    web_search_requests: int = 0


class UsageTotals(BaseModel):
    """Aggregate over the FULL filtered set (not just the current page).

    Mirrors UsageRow's counter fields. ``row_count`` is the number of
    rows that would be returned across all pages combined — useful
    for the UI to show "showing 50 of 137".
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    web_search_requests: int = 0
    row_count: int = 0


class UsageListResponse(BaseModel):
    rows: list[UsageRow]
    totals: UsageTotals
    next_cursor: Optional[str] = None


# ───────────────────────────────────────────────────────────────────────────
# Cursor encoding
# ───────────────────────────────────────────────────────────────────────────
#
# Cursor identifies "the last row returned" so the next page starts
# just after it. Sort key is (occurred_at DESC, record_id DESC,
# ordinality ASC); strictly-greater-than in the DESC dimensions and
# strictly-less in the ASC dimension. Wrapped in base64url so the
# client treats it as opaque.


def _encode_cursor(occurred_at: datetime, record_id: str, ordinality: int) -> str:
    payload = json.dumps(
        {
            "ts": occurred_at.isoformat(),
            "rid": record_id,
            "ord": ordinality,
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(
        payload.encode("utf-8")
    ).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(padded.encode("ascii"))
        )
        return (
            datetime.fromisoformat(payload["ts"]),
            str(payload["rid"]),
            int(payload["ord"]),
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
# GET /usage
# ───────────────────────────────────────────────────────────────────────────


# How far back to look by default. Matches the 90-day onboarding
# backfill ceiling.
_DEFAULT_LOOKBACK_DAYS = 30


@router.get(
    "/usage",
    response_model=UsageListResponse,
    operation_id="listUsage",
    tags=["usage"],
    summary="List Admin API usage rows for the authenticated tenant",
)
def list_usage(
    cursor: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    since: Optional[date] = Query(None),
    until: Optional[date] = Query(None),
    workspace_id: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
    user: AuthenticatedUser = Depends(current_user),
) -> UsageListResponse:
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "no_tenant_bound",
                "message": "Your session is not bound to a tenant yet.",
            },
        )

    # Date range defaults. `since` defaults to N days before `until`
    # so customers see roughly "the last month of activity" without
    # having to think about it.
    today_utc = datetime.now(tz=None).date()
    if until is None:
        until = today_utc
    if since is None:
        since = until - timedelta(days=_DEFAULT_LOOKBACK_DAYS - 1)
    if since > until:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_date_range",
                "message": f"since ({since}) must be <= until ({until}).",
            },
        )

    # Build the WHERE clause shared by both the page query and the
    # totals query. RLS via session_scope handles tenant_id; this
    # filters to admin usage records in the requested date range.
    params: dict[str, Any] = {
        "since_ts": datetime.combine(since, datetime.min.time()),
        # Inclusive upper bound on the date: start-of-day-after.
        "until_ts": datetime.combine(
            until + timedelta(days=1), datetime.min.time()
        ),
    }

    base_where = (
        "tenant_id = current_setting('app.tenant_id') "
        "AND record_type = 'usage' "
        "AND source_api = 'admin' "
        "AND occurred_at >= :since_ts "
        "AND occurred_at < :until_ts"
    )

    # Per-row filters apply against the expanded jsonb array. They
    # live in the expanded CTE, not the base record filter, because
    # a single record may have multiple result groups with different
    # workspace_id / model values.
    expanded_filters: list[str] = []
    if workspace_id is not None:
        expanded_filters.append("(result->>'workspace_id') = :workspace_id_filter")
        params["workspace_id_filter"] = workspace_id
    if model is not None:
        expanded_filters.append("(result->>'model') = :model_filter")
        params["model_filter"] = model
    expanded_where = (
        ("AND " + " AND ".join(expanded_filters)) if expanded_filters else ""
    )

    # Cursor filter — strictly after the cursor row in the sort order.
    # Sort: occurred_at DESC, record_id DESC, ordinality ASC.
    #
    # "Strictly after" in mixed DESC/ASC requires the case-split:
    #   (occurred_at < cursor_ts)
    #   OR (occurred_at = cursor_ts AND record_id < cursor_rid)
    #   OR (occurred_at = cursor_ts AND record_id = cursor_rid AND ordinality > cursor_ord)
    cursor_clause = ""
    if cursor is not None:
        cursor_ts, cursor_rid, cursor_ord = _decode_cursor(cursor)
        cursor_clause = (
            "AND ("
            "  occurred_at < CAST(:cursor_ts AS timestamptz) "
            "  OR (occurred_at = CAST(:cursor_ts AS timestamptz) "
            "      AND id::text < CAST(:cursor_rid AS text)) "
            "  OR (occurred_at = CAST(:cursor_ts AS timestamptz) "
            "      AND id::text = CAST(:cursor_rid AS text) "
            "      AND ordinality > CAST(:cursor_ord AS bigint))"
            ")"
        )
        params["cursor_ts"] = cursor_ts
        params["cursor_rid"] = cursor_rid
        params["cursor_ord"] = cursor_ord

    # Expanded query: jsonb_array_elements_with_ordinality flattens
    # metadata.results into one row per element. ordinality starts at
    # 1 and is stable per record (input array order is preserved).
    page_sql = f"""
        WITH expanded AS (
            SELECT
                tr.id::text AS record_id,
                tr.occurred_at,
                DATE(tr.occurred_at AT TIME ZONE 'UTC') AS row_date,
                r.result,
                r.ordinality
            FROM telemetry_records tr,
                 jsonb_array_elements(tr.metadata->'results')
                     WITH ORDINALITY AS r(result, ordinality)
            WHERE {base_where}
              {expanded_where}
              {cursor_clause}
        )
        SELECT
            row_date,
            record_id,
            occurred_at,
            ordinality,
            result->>'workspace_id' AS workspace_id,
            result->>'model' AS model,
            COALESCE((result->>'input_tokens')::bigint, 0) AS input_tokens,
            COALESCE((result->>'output_tokens')::bigint, 0) AS output_tokens,
            COALESCE((result->>'cache_read_input_tokens')::bigint, 0) AS cache_read_tokens,
            COALESCE((result->>'cache_creation_input_tokens')::bigint, 0) AS cache_creation_tokens,
            COALESCE(
                ((result->'server_tool_use')->>'web_search_requests')::bigint, 0
            ) AS web_search_requests
        FROM expanded
        ORDER BY occurred_at DESC, record_id DESC, ordinality ASC
        LIMIT :limit + 1
    """
    params["limit"] = limit

    # Totals query: same filters minus pagination + cursor. SUM over
    # the entire filtered set; row_count is the COUNT of expanded
    # groups (NOT records).
    totals_sql = f"""
        WITH expanded AS (
            SELECT r.result
            FROM telemetry_records tr,
                 jsonb_array_elements(tr.metadata->'results')
                     WITH ORDINALITY AS r(result, ordinality)
            WHERE {base_where}
              {expanded_where}
        )
        SELECT
            COUNT(*) AS row_count,
            COALESCE(SUM((result->>'input_tokens')::bigint), 0) AS input_tokens,
            COALESCE(SUM((result->>'output_tokens')::bigint), 0) AS output_tokens,
            COALESCE(
                SUM((result->>'cache_read_input_tokens')::bigint), 0
            ) AS cache_read_tokens,
            COALESCE(
                SUM((result->>'cache_creation_input_tokens')::bigint), 0
            ) AS cache_creation_tokens,
            COALESCE(
                SUM(((result->'server_tool_use')->>'web_search_requests')::bigint),
                0
            ) AS web_search_requests
        FROM expanded
    """

    with session_scope(user.tenant_id) as s:
        page_result = s.execute(sql_text(page_sql), params).all()
        # Totals query uses a strict subset of params (no limit, no
        # cursor) — pass the same params dict; SQLAlchemy ignores
        # unreferenced keys.
        totals_row = s.execute(sql_text(totals_sql), params).one()

    has_more = len(page_result) > limit
    page_rows_raw = page_result[:limit]

    next_cursor: Optional[str] = None
    if has_more and page_rows_raw:
        last = page_rows_raw[-1]
        next_cursor = _encode_cursor(
            last.occurred_at, last.record_id, int(last.ordinality)
        )

    rows = [
        UsageRow(
            row_date=r.row_date,
            workspace_id=r.workspace_id,
            model=r.model,
            input_tokens=int(r.input_tokens),
            output_tokens=int(r.output_tokens),
            cache_read_tokens=int(r.cache_read_tokens),
            cache_creation_tokens=int(r.cache_creation_tokens),
            web_search_requests=int(r.web_search_requests),
        )
        for r in page_rows_raw
    ]

    totals = UsageTotals(
        input_tokens=int(totals_row.input_tokens),
        output_tokens=int(totals_row.output_tokens),
        cache_read_tokens=int(totals_row.cache_read_tokens),
        cache_creation_tokens=int(totals_row.cache_creation_tokens),
        web_search_requests=int(totals_row.web_search_requests),
        row_count=int(totals_row.row_count),
    )

    return UsageListResponse(rows=rows, totals=totals, next_cursor=next_cursor)


__all__ = ["router"]
