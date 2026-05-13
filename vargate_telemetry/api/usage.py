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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text

from vargate_telemetry.auth.middleware import AuthenticatedUser, current_user
from vargate_telemetry.db import session_scope
from vargate_telemetry.pricing import compute_cost_usd


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

    ``estimated_cost_usd`` is ``None`` (rendered as ``—`` in the UI)
    when ``model`` is null (legacy aggregate rows) or unknown to the
    rate card. **Never faked** — a wrong dollar figure on a
    dashboard is worse than no figure.

    ``workspace_name`` is resolved from the ``workspaces`` side
    table (populated by the backfill / pull task via
    ``client.list_workspaces()``). ``None`` when not yet resolved or
    when ``workspace_id`` is null.
    """

    model_config = {"populate_by_name": True}

    row_date: date = Field(alias="date")
    workspace_id: Optional[str] = None
    workspace_name: Optional[str] = None
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    web_search_requests: int = 0
    estimated_cost_usd: Optional[Decimal] = None


class UsageTotals(BaseModel):
    """Aggregate over the FULL filtered set (not just the current page).

    Mirrors UsageRow's counter fields. ``row_count`` is the number of
    rows that would be returned across all pages combined — useful
    for the UI to show "showing 50 of 137".

    ``total_cost_usd`` sums every row's ``estimated_cost_usd`` where
    the model was known; rows that returned ``None`` simply don't
    contribute. The UI shows the figure as "≥ $X" rendering when
    ``rows_without_cost > 0`` so customers know the number is a
    floor, not a ceiling.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    web_search_requests: int = 0
    row_count: int = 0
    total_cost_usd: Optional[Decimal] = None
    rows_without_cost: int = 0


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
    # T5.5.7 raised the cap from 200 → 1000 so chart fetches can pull
    # ~30 days of per-model breakdown in one round-trip. Charts use
    # the same /usage endpoint the table reads from — no separate
    # /chart shape (see vargate-frontend CLAUDE.md).
    limit: int = Query(50, ge=1, le=1000),
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

    # T5.5.6: suppress legacy aggregate rows (model=null, ingested
    # before the connector started passing group_by) on any date that
    # ALSO has per-model breakdown rows. Both shapes coexist in
    # ``telemetry_records`` because the per-model external_id format
    # doesn't collide with the legacy external_id, so a re-pull
    # writes new rows without disturbing the old ones — that's the
    # right behaviour at the audit-chain layer, but the dashboard
    # would otherwise double-count and confuse customers. Filter at
    # the API view, not the data layer.
    #
    # The subquery EXISTS check is per-date: a date that has even one
    # per-model row hides ALL of its legacy aggregates; a date that
    # has only legacy rows (pre-backfill state, or genuinely zero
    # activity recorded as an empty-results bucket) keeps them so
    # the view doesn't go blank.
    supersession_filter = """
        AND NOT (
            (r.result->>'model') IS NULL
            AND EXISTS (
                SELECT 1
                FROM telemetry_records tr2,
                     jsonb_array_elements(tr2.metadata->'results')
                         AS r2(result)
                WHERE tr2.tenant_id = current_setting('app.tenant_id')
                  AND tr2.record_type = 'usage'
                  AND tr2.source_api = 'admin'
                  AND DATE(tr2.occurred_at AT TIME ZONE 'UTC')
                      = DATE(tr.occurred_at AT TIME ZONE 'UTC')
                  AND (r2.result->>'model') IS NOT NULL
            )
        )
    """

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
    # `cache_creation_total` collapses the nested `cache_creation`
    # (ephemeral_5m + ephemeral_1h) variant T5.5.6 receives from the
    # group_by'd response with the legacy flat `cache_creation_input_tokens`
    # field — COALESCE picks whichever is populated.
    # Workspace-name resolution: LEFT JOIN on the `workspaces` table.
    # Always LEFT JOIN — most tenants have null workspace_id today
    # (Personal plan; no workspaces created) and we still want their
    # rows.
    page_sql = f"""
        WITH expanded AS (
            SELECT
                tr.id::text AS record_id,
                tr.occurred_at,
                DATE(tr.occurred_at AT TIME ZONE 'UTC') AS row_date,
                tr.tenant_id,
                r.result,
                r.ordinality
            FROM telemetry_records tr,
                 jsonb_array_elements(tr.metadata->'results')
                     WITH ORDINALITY AS r(result, ordinality)
            WHERE {base_where}
              {expanded_where}
              {cursor_clause}
              {supersession_filter}
        )
        SELECT
            e.row_date,
            e.record_id,
            e.occurred_at,
            e.ordinality,
            e.result->>'workspace_id' AS workspace_id,
            w.name AS workspace_name,
            e.result->>'model' AS model,
            COALESCE((e.result->>'input_tokens')::bigint, 0) AS input_tokens,
            COALESCE((e.result->>'output_tokens')::bigint, 0) AS output_tokens,
            COALESCE((e.result->>'cache_read_input_tokens')::bigint, 0) AS cache_read_tokens,
            COALESCE(
                -- NULLIF: when the flat field is 0 (Pydantic default
                -- because the group_by'd response shape DROPPED the
                -- flat key entirely; UsageBreakdown.cache_creation_input_tokens
                -- defaults to 0 → serializes as 0 even when the real
                -- value is in the nested dict), fall through to the
                -- nested sum. Without NULLIF, COALESCE picks the
                -- non-null 0 and never reads the nested field.
                NULLIF((e.result->>'cache_creation_input_tokens')::bigint, 0),
                ((e.result->'cache_creation')->>'ephemeral_5m_input_tokens')::bigint
                + ((e.result->'cache_creation')->>'ephemeral_1h_input_tokens')::bigint,
                0
            ) AS cache_creation_tokens,
            COALESCE(
                ((e.result->'server_tool_use')->>'web_search_requests')::bigint, 0
            ) AS web_search_requests
        FROM expanded e
        LEFT JOIN workspaces w
          ON w.tenant_id = e.tenant_id
         AND w.workspace_id = (e.result->>'workspace_id')
        ORDER BY e.occurred_at DESC, e.record_id DESC, e.ordinality ASC
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
              {supersession_filter}
        )
        SELECT
            COUNT(*) AS row_count,
            COALESCE(SUM((result->>'input_tokens')::bigint), 0) AS input_tokens,
            COALESCE(SUM((result->>'output_tokens')::bigint), 0) AS output_tokens,
            COALESCE(
                SUM((result->>'cache_read_input_tokens')::bigint), 0
            ) AS cache_read_tokens,
            COALESCE(
                SUM(COALESCE(
                    NULLIF((result->>'cache_creation_input_tokens')::bigint, 0),
                    ((result->'cache_creation')->>'ephemeral_5m_input_tokens')::bigint
                    + ((result->'cache_creation')->>'ephemeral_1h_input_tokens')::bigint,
                    0
                )),
                0
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

    rows: list[UsageRow] = []
    for r in page_rows_raw:
        # Force UTC tz for the cost lookup; occurred_at from Postgres
        # always carries tzinfo, but Pydantic-level callers might pass
        # in naive datetimes via the unit-test path.
        occurred = r.occurred_at
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        cost = compute_cost_usd(
            r.model,
            input_tokens=int(r.input_tokens),
            output_tokens=int(r.output_tokens),
            cache_read_tokens=int(r.cache_read_tokens),
            cache_creation_tokens=int(r.cache_creation_tokens),
            occurred_at=occurred,
        )
        rows.append(
            UsageRow(
                row_date=r.row_date,
                workspace_id=r.workspace_id,
                workspace_name=r.workspace_name,
                model=r.model,
                input_tokens=int(r.input_tokens),
                output_tokens=int(r.output_tokens),
                cache_read_tokens=int(r.cache_read_tokens),
                cache_creation_tokens=int(r.cache_creation_tokens),
                web_search_requests=int(r.web_search_requests),
                estimated_cost_usd=cost,
            )
        )

    # Totals cost: a second pass that walks the expanded set with the
    # per-model rate. The page cost lives on each row; the TOTALS
    # cost has to aggregate across pages. Run a small per-model SUM
    # over the same filtered set and apply rates server-side. This is
    # a separate query so it doesn't bloat the page query's row
    # shape, and the per-model SUM stays bounded (one row per model
    # ever active in the window).
    cost_by_model_sql = f"""
        WITH expanded AS (
            SELECT r.result, tr.occurred_at
            FROM telemetry_records tr,
                 jsonb_array_elements(tr.metadata->'results')
                     WITH ORDINALITY AS r(result, ordinality)
            WHERE {base_where}
              {expanded_where}
              {supersession_filter}
        )
        SELECT
            result->>'model' AS model,
            MIN(occurred_at) AS earliest_occurred_at,
            COUNT(*) AS row_count,
            COALESCE(SUM((result->>'input_tokens')::bigint), 0) AS input_tokens,
            COALESCE(SUM((result->>'output_tokens')::bigint), 0) AS output_tokens,
            COALESCE(
                SUM((result->>'cache_read_input_tokens')::bigint), 0
            ) AS cache_read_tokens,
            COALESCE(
                SUM(COALESCE(
                    -- Same NULLIF fix as the page + totals queries:
                    -- the Pydantic default 0 must NOT mask the real
                    -- nested cache_creation values.
                    NULLIF((result->>'cache_creation_input_tokens')::bigint, 0),
                    ((result->'cache_creation')->>'ephemeral_5m_input_tokens')::bigint
                    + ((result->'cache_creation')->>'ephemeral_1h_input_tokens')::bigint,
                    0
                )),
                0
            ) AS cache_creation_tokens
        FROM expanded
        GROUP BY result->>'model'
    """

    with session_scope(user.tenant_id) as s2:
        cost_buckets = s2.execute(sql_text(cost_by_model_sql), params).all()

    total_cost = Decimal("0")
    rows_without_cost = 0
    any_cost_computed = False
    for cb in cost_buckets:
        occurred = cb.earliest_occurred_at
        if occurred is not None and occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        elif occurred is None:
            # No rows in this bucket — skip.
            continue
        bucket_cost = compute_cost_usd(
            cb.model,
            input_tokens=int(cb.input_tokens),
            output_tokens=int(cb.output_tokens),
            cache_read_tokens=int(cb.cache_read_tokens),
            cache_creation_tokens=int(cb.cache_creation_tokens),
            occurred_at=occurred,
        )
        if bucket_cost is None:
            rows_without_cost += int(cb.row_count)
        else:
            any_cost_computed = True
            total_cost += bucket_cost

    totals = UsageTotals(
        input_tokens=int(totals_row.input_tokens),
        output_tokens=int(totals_row.output_tokens),
        cache_read_tokens=int(totals_row.cache_read_tokens),
        cache_creation_tokens=int(totals_row.cache_creation_tokens),
        web_search_requests=int(totals_row.web_search_requests),
        row_count=int(totals_row.row_count),
        total_cost_usd=(
            total_cost.quantize(Decimal("0.01")) if any_cost_computed else None
        ),
        rows_without_cost=rows_without_cost,
    )

    return UsageListResponse(rows=rows, totals=totals, next_cursor=next_cursor)


__all__ = ["router"]
