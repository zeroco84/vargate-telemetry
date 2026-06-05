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
from vargate_telemetry.pricing import compute_cost_usd, openai_rates
from vargate_telemetry.pricing.vendor_cost import (
    SOURCE_API_ANTHROPIC_USAGE,
    SOURCE_API_OPENAI_USAGE,
)


_log = logging.getLogger(__name__)

router = APIRouter()


# ───────────────────────────────────────────────────────────────────────────
# Response shapes
# ───────────────────────────────────────────────────────────────────────────


class UsageRow(BaseModel):
    """One (date, workspace_id/project_id, model) breakdown row.

    All token counters are ``int`` because neither vendor's usage API
    returns fractional tokens; ``None`` for the breakdown keys means
    the connector didn't request ``group_by`` for that dimension —
    not "all workspaces" semantically.

    ``estimated_cost_usd`` is ``None`` (rendered as ``—`` in the UI)
    when ``model`` is null (legacy aggregate rows) or unknown to the
    rate card. **Never faked** — a wrong dollar figure on a
    dashboard is worse than no figure.

    ``workspace_name`` is resolved from the vendor's side table —
    ``workspaces`` for Anthropic, ``openai_projects`` for OpenAI
    (the column is the "Project / Workspace" dimension in the UI).
    ``None`` when not yet resolved or when ``workspace_id`` is null.

    TM3 Phase A4: ``api_key_id`` + ``api_key_name`` — the rows surface
    which API key drove each (date, model, workspace_id) breakdown.
    ``api_key_name`` resolves via a LEFT JOIN onto the vendor's keys
    side table (``api_keys`` for Anthropic, ``openai_api_keys`` for
    OpenAI).

    TM8 Phase D: ``source_api`` carries the ingest stream this row came
    from — ``"admin"`` (Anthropic usage) or ``"openai_admin_usage"``
    (OpenAI usage). The frontend derives the vendor badge from it via
    ``sourceVendor()``. Pre-TM8 the endpoint returned only ``admin``
    rows; that behavior is unchanged for Anthropic tenants — the field
    is purely additive (every legacy row now reports ``"admin"``).

    OpenAI token mapping (recon §2.1 double-count trap): for an
    ``openai_admin_usage`` row the counters are the BILLABLE split, not
    the raw wire fields — ``input_tokens`` is the uncached portion
    (``input_uncached_tokens``), ``cache_read_tokens`` is the cached
    portion (``input_cached_tokens``), and ``cache_creation_tokens`` is
    always 0 (OpenAI has no cache-write charge). So the same counter
    columns mean "full-rate input / cached input / cache-write" for
    both vendors and ``estimated_cost_usd`` is consistent with them.
    ``web_search_requests`` is an Anthropic-only server-tool counter; it
    stays 0 for OpenAI rows.
    """

    model_config = {"populate_by_name": True}

    row_date: date = Field(alias="date")
    source_api: str = SOURCE_API_ANTHROPIC_USAGE
    workspace_id: Optional[str] = None
    workspace_name: Optional[str] = None
    api_key_id: Optional[str] = None
    api_key_name: Optional[str] = None
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
# Cross-vendor expanded CTE (TM8 Phase D)
# ───────────────────────────────────────────────────────────────────────────
#
# The page / totals / cost queries all read from one expanded set built
# by UNION ALL of two vendor branches. Both branches emit the SAME
# normalized columns so everything downstream is vendor-agnostic:
#
#   record_id, occurred_at, row_date, ordinality, source_api,
#   workspace_id, workspace_name, api_key_id, api_key_name, model,
#   input_tokens, output_tokens, cache_read_tokens,
#   cache_creation_tokens, web_search_requests
#
# Anthropic branch (source_api='admin'): one row per
# ``metadata->'results'`` element, preserving the T5.5.6 supersession
# filter + the cache-creation NULLIF fallback bit-for-bit — an
# Anthropic-only tenant gets the exact pre-TM8 result set.
#
# OpenAI branch (source_api='openai_admin_usage'): one row per record
# reading the single ``metadata->'result'`` object. Token columns carry
# the BILLABLE split (recon §2.1 double-count trap): input_tokens =
# input_uncached_tokens (full rate), cache_read_tokens =
# input_cached_tokens (cached), cache_creation_tokens = 0 (OpenAI has no
# cache-write charge), output_tokens = output_tokens. The "workspace"
# dimension is OpenAI's project_id, resolved to a name via
# ``openai_projects``; api_key_id resolves via ``openai_api_keys``.
# Empty-bucket sentinels (``metadata->'result'`` is JSON null) are
# excluded — they carry no dimension and aren't real usage rows.


def _anthropic_branch(
    *, has_cursor: bool, has_workspace_filter: bool, has_model_filter: bool
) -> str:
    """Anthropic half of the union. Byte-for-byte the pre-TM8 logic,
    re-projected to the shared column shape (adds the ``source_api``
    literal). Filters/cursor reference the same bind params as the
    OpenAI branch so both halves stay parameter-compatible."""
    where = [
        "tr.tenant_id = current_setting('app.tenant_id')",
        "tr.record_type = 'usage'",
        "tr.source_api = 'admin'",
        "tr.occurred_at >= :since_ts",
        "tr.occurred_at < :until_ts",
    ]
    if has_workspace_filter:
        where.append("(r.result->>'workspace_id') = :workspace_id_filter")
    if has_model_filter:
        where.append("(r.result->>'model') = :model_filter")
    if has_cursor:
        where.append(
            "("
            "  tr.occurred_at < CAST(:cursor_ts AS timestamptz) "
            "  OR (tr.occurred_at = CAST(:cursor_ts AS timestamptz) "
            "      AND tr.id::text < CAST(:cursor_rid AS text)) "
            "  OR (tr.occurred_at = CAST(:cursor_ts AS timestamptz) "
            "      AND tr.id::text = CAST(:cursor_rid AS text) "
            "      AND r.ordinality > CAST(:cursor_ord AS bigint))"
            ")"
        )
    # T5.5.6 supersession: hide legacy aggregate rows (model=null) on any
    # UTC date that ALSO has a per-model breakdown row, so the two shapes
    # don't double-count. Per-date EXISTS — a date with only legacy rows
    # keeps them so the view doesn't go blank.
    where.append(
        """NOT (
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
        )"""
    )
    where_sql = "\n              AND ".join(where)
    return f"""
        SELECT
            tr.id::text AS record_id,
            tr.occurred_at,
            DATE(tr.occurred_at AT TIME ZONE 'UTC') AS row_date,
            r.ordinality,
            'admin' AS source_api,
            r.result->>'workspace_id' AS workspace_id,
            w.name AS workspace_name,
            r.result->>'api_key_id' AS api_key_id,
            ak.name AS api_key_name,
            r.result->>'model' AS model,
            COALESCE((r.result->>'input_tokens')::bigint, 0) AS input_tokens,
            COALESCE((r.result->>'output_tokens')::bigint, 0) AS output_tokens,
            COALESCE((r.result->>'cache_read_input_tokens')::bigint, 0)
                AS cache_read_tokens,
            COALESCE(
                -- NULLIF: a flat 0 (Pydantic default from the group_by'd
                -- response that DROPPED the flat key) must NOT mask the
                -- real nested cache_creation sum.
                NULLIF((r.result->>'cache_creation_input_tokens')::bigint, 0),
                ((r.result->'cache_creation')->>'ephemeral_5m_input_tokens')::bigint
                + ((r.result->'cache_creation')->>'ephemeral_1h_input_tokens')::bigint,
                0
            ) AS cache_creation_tokens,
            COALESCE(
                ((r.result->'server_tool_use')->>'web_search_requests')::bigint, 0
            ) AS web_search_requests
        FROM (telemetry_records tr
              CROSS JOIN jsonb_array_elements(tr.metadata->'results')
                  WITH ORDINALITY AS r(result, ordinality))
        LEFT JOIN workspaces w
          ON w.tenant_id = tr.tenant_id
         AND w.workspace_id = (r.result->>'workspace_id')
        LEFT JOIN api_keys ak
          ON ak.tenant_id = tr.tenant_id
         AND ak.api_key_id = (r.result->>'api_key_id')
        WHERE {where_sql}
    """


def _openai_branch(
    *, has_cursor: bool, has_workspace_filter: bool, has_model_filter: bool
) -> str:
    """OpenAI half of the union (TM8). One row per ``openai_admin_usage``
    record (``metadata->'result'`` is a single object, so ordinality is
    a constant 1). Token columns carry the billable split; the
    "workspace" dimension is the OpenAI project."""
    where = [
        "tr.tenant_id = current_setting('app.tenant_id')",
        "tr.record_type = 'usage'",
        "tr.source_api = 'openai_admin_usage'",
        "tr.occurred_at >= :since_ts",
        "tr.occurred_at < :until_ts",
        # Exclude empty-bucket sentinels (result is JSON null): they
        # carry no model/dimension and would be meaningless rows.
        "jsonb_typeof(tr.metadata->'result') = 'object'",
    ]
    if has_workspace_filter:
        where.append(
            "((tr.metadata->'result')->>'project_id') = :workspace_id_filter"
        )
    if has_model_filter:
        where.append(
            "((tr.metadata->'result')->>'model') = :model_filter"
        )
    if has_cursor:
        # ordinality is constant 1 for OpenAI; the (ordinality > cursor)
        # arm can only fire when the cursor's ordinality is 0, which a
        # real cursor never carries — so an OpenAI row never re-appears
        # via that arm. The first two arms (occurred_at / id) do the
        # real work, same as Anthropic.
        where.append(
            "("
            "  tr.occurred_at < CAST(:cursor_ts AS timestamptz) "
            "  OR (tr.occurred_at = CAST(:cursor_ts AS timestamptz) "
            "      AND tr.id::text < CAST(:cursor_rid AS text)) "
            "  OR (tr.occurred_at = CAST(:cursor_ts AS timestamptz) "
            "      AND tr.id::text = CAST(:cursor_rid AS text) "
            "      AND 1 > CAST(:cursor_ord AS bigint))"
            ")"
        )
    where_sql = "\n              AND ".join(where)
    return f"""
        SELECT
            tr.id::text AS record_id,
            tr.occurred_at,
            DATE(tr.occurred_at AT TIME ZONE 'UTC') AS row_date,
            1::bigint AS ordinality,
            'openai_admin_usage' AS source_api,
            -- OpenAI's project_id IS the "Project / Workspace" dimension.
            (tr.metadata->'result')->>'project_id' AS workspace_id,
            op.name AS workspace_name,
            (tr.metadata->'result')->>'api_key_id' AS api_key_id,
            ok.name AS api_key_name,
            (tr.metadata->'result')->>'model' AS model,
            -- §2.1 double-count trap: bill the UNCACHED portion as
            -- input, the CACHED portion as cache_read, NEVER the raw
            -- input_tokens total (which includes the cached part).
            COALESCE(
                ((tr.metadata->'result')->>'input_uncached_tokens')::bigint, 0
            ) AS input_tokens,
            COALESCE(
                ((tr.metadata->'result')->>'output_tokens')::bigint, 0
            ) AS output_tokens,
            COALESCE(
                ((tr.metadata->'result')->>'input_cached_tokens')::bigint, 0
            ) AS cache_read_tokens,
            -- OpenAI has no cache-write charge → always 0.
            0::bigint AS cache_creation_tokens,
            -- web_search_requests is an Anthropic-only server-tool
            -- counter; 0 for OpenAI rows.
            0::bigint AS web_search_requests
        FROM telemetry_records tr
        LEFT JOIN openai_projects op
          ON op.tenant_id = tr.tenant_id
         AND op.project_id = ((tr.metadata->'result')->>'project_id')
        LEFT JOIN openai_api_keys ok
          ON ok.tenant_id = tr.tenant_id
         AND ok.api_key_id = ((tr.metadata->'result')->>'api_key_id')
        WHERE {where_sql}
    """


def _build_expanded_cte(
    *, has_cursor: bool, has_workspace_filter: bool, has_model_filter: bool
) -> str:
    """The UNION ALL body shared by the page, totals, and cost queries.

    ``has_cursor`` is False for the totals + cost queries (they aggregate
    the whole filtered set, not a page). The filter flags gate the
    optional WHERE predicates so the bind params only need supplying
    when the corresponding query arg was passed.
    """
    anthropic = _anthropic_branch(
        has_cursor=has_cursor,
        has_workspace_filter=has_workspace_filter,
        has_model_filter=has_model_filter,
    )
    openai = _openai_branch(
        has_cursor=has_cursor,
        has_workspace_filter=has_workspace_filter,
        has_model_filter=has_model_filter,
    )
    return f"{anthropic}\n        UNION ALL\n{openai}"


def _price_usage_row(
    source_api: str,
    *,
    model: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    occurred_at: datetime,
) -> Optional[Decimal]:
    """Price one already-normalized usage row by vendor.

    The token args are the BILLABLE split for both vendors (the OpenAI
    branch already mapped uncached→input, cached→cache_read, 0→creation),
    so this just selects the rate card by ``source_api`` and delegates.
    Anthropic rows go through the unchanged ``compute_cost_usd`` — the
    Anthropic numbers are byte-identical to pre-TM8. ``None`` for an
    unknown/None model (never faked), same discipline as the underlying
    helpers.
    """
    if source_api == SOURCE_API_OPENAI_USAGE:
        return openai_rates.compute_cost_usd(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            occurred_at=occurred_at,
        )
    # Anthropic (``admin``) and any other stream that reaches here use
    # the Anthropic rate card — unchanged from pre-TM8.
    return compute_cost_usd(
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        occurred_at=occurred_at,
    )


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

    # Bind params shared by the page / totals / cost queries. RLS via
    # session_scope handles tenant_id; the date window restricts both
    # vendor branches of the expanded CTE to the requested range.
    params: dict[str, Any] = {
        "since_ts": datetime.combine(since, datetime.min.time()),
        # Inclusive upper bound on the date: start-of-day-after.
        "until_ts": datetime.combine(
            until + timedelta(days=1), datetime.min.time()
        ),
    }

    # Per-row filters. ``workspace_id`` filters on the workspace
    # dimension for Anthropic and the project_id dimension for OpenAI
    # (the UI labels the merged column "Project / Workspace"); ``model``
    # filters both. The bind params are shared across both branches of
    # the UNION below.
    if workspace_id is not None:
        params["workspace_id_filter"] = workspace_id
    if model is not None:
        params["model_filter"] = model

    # Cursor filter — strictly after the cursor row in the sort order.
    # Sort: occurred_at DESC, record_id DESC, ordinality ASC. Applied
    # identically in BOTH branches of the union (the sort columns are
    # vendor-neutral) so a page can straddle the two streams.
    #
    # "Strictly after" in mixed DESC/ASC requires the case-split:
    #   (occurred_at < cursor_ts)
    #   OR (occurred_at = cursor_ts AND record_id < cursor_rid)
    #   OR (occurred_at = cursor_ts AND record_id = cursor_rid AND ordinality > cursor_ord)
    if cursor is not None:
        cursor_ts, cursor_rid, cursor_ord = _decode_cursor(cursor)
        params["cursor_ts"] = cursor_ts
        params["cursor_rid"] = cursor_rid
        params["cursor_ord"] = cursor_ord

    # The shared expanded CTE: a UNION ALL of the Anthropic branch
    # (``source_api='admin'``, one row per ``metadata->'results'``
    # element, with the T5.5.6 supersession + cache-creation NULLIF
    # handling preserved bit-for-bit) and the OpenAI branch
    # (``source_api='openai_admin_usage'``, one row per record reading
    # the single ``metadata->'result'`` object). Both branches project
    # the SAME normalized columns so the page / totals / cost queries
    # are vendor-agnostic from here on. The Anthropic branch's output
    # for an Anthropic-only tenant is identical to the pre-TM8 query.
    expanded_cte = _build_expanded_cte(
        has_cursor=cursor is not None,
        has_workspace_filter=workspace_id is not None,
        has_model_filter=model is not None,
    )

    page_sql = f"""
        WITH expanded AS (
            {expanded_cte}
        )
        SELECT
            e.row_date,
            e.record_id,
            e.occurred_at,
            e.ordinality,
            e.source_api,
            e.workspace_id,
            e.workspace_name,
            e.api_key_id,
            e.api_key_name,
            e.model,
            e.input_tokens,
            e.output_tokens,
            e.cache_read_tokens,
            e.cache_creation_tokens,
            e.web_search_requests
        FROM expanded e
        ORDER BY e.occurred_at DESC, e.record_id DESC, e.ordinality ASC
        LIMIT :limit + 1
    """
    params["limit"] = limit

    # Totals query: same filtered set minus pagination + cursor. SUM
    # over the whole set; row_count is the COUNT of expanded groups
    # (NOT records). Reads the same expanded CTE — without the cursor
    # clause — so the merged totals are correct across both vendors.
    totals_cte = _build_expanded_cte(
        has_cursor=False,
        has_workspace_filter=workspace_id is not None,
        has_model_filter=model is not None,
    )
    totals_sql = f"""
        WITH expanded AS (
            {totals_cte}
        )
        SELECT
            COUNT(*) AS row_count,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
            COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
            COALESCE(SUM(web_search_requests), 0) AS web_search_requests
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
        # Vendor-aware pricing. The row's token columns are already the
        # billable split for BOTH vendors (the OpenAI branch mapped
        # input_uncached → input_tokens, input_cached → cache_read,
        # cache_creation → 0), so the right rate card just needs picking
        # by source_api. Anthropic rows go through the unchanged
        # ``compute_cost_usd`` — identical numbers to pre-TM8.
        cost = _price_usage_row(
            r.source_api,
            model=r.model,
            input_tokens=int(r.input_tokens),
            output_tokens=int(r.output_tokens),
            cache_read_tokens=int(r.cache_read_tokens),
            cache_creation_tokens=int(r.cache_creation_tokens),
            occurred_at=occurred,
        )
        rows.append(
            UsageRow(
                row_date=r.row_date,
                source_api=r.source_api,
                workspace_id=r.workspace_id,
                workspace_name=r.workspace_name,
                api_key_id=r.api_key_id,
                api_key_name=r.api_key_name,
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
    # per-(vendor, model) rate. The page cost lives on each row; the
    # TOTALS cost has to aggregate across pages. Run a small per-model
    # SUM over the same filtered set and apply rates server-side. This
    # is a separate query so it doesn't bloat the page query's row
    # shape, and the per-model SUM stays bounded (one row per (vendor,
    # model) ever active in the window).
    #
    # Grouped by ``source_api`` too so each bucket is priced with the
    # right vendor's rate card (``_price_usage_row`` dispatches). The
    # ``MIN(occurred_at)`` anchor per bucket is unchanged from the
    # pre-TM8 Anthropic path — so an Anthropic-only tenant's
    # ``total_cost_usd`` is byte-identical (every Anthropic bucket
    # prices exactly as before; adding the source_api grouping doesn't
    # split an Anthropic bucket because every Anthropic row already
    # carries source_api='admin').
    cost_cte = _build_expanded_cte(
        has_cursor=False,
        has_workspace_filter=workspace_id is not None,
        has_model_filter=model is not None,
    )
    cost_by_model_sql = f"""
        WITH expanded AS (
            {cost_cte}
        )
        SELECT
            source_api,
            model,
            MIN(occurred_at) AS earliest_occurred_at,
            COUNT(*) AS row_count,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
            COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens
        FROM expanded
        GROUP BY source_api, model
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
        bucket_cost = _price_usage_row(
            cb.source_api,
            model=cb.model,
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


# ───────────────────────────────────────────────────────────────────────────
# Cache-efficiency recommendations (TM5 T5.5)
#
# Pure analysis over the already-captured Admin-API usage records — no
# ingest, no schema. A model's input splits into uncached
# (`input_tokens`), cache-read (reused), and cache-creation (written);
# hit_rate = read / (read + creation) is how much of the cache we paid to
# WRITE actually gets REUSED. Below this input-token floor over the
# window, caching impact is negligible, so we don't nag.
# ───────────────────────────────────────────────────────────────────────────

_CACHE_VOLUME_FLOOR = 100_000


class CacheModelStat(BaseModel):
    model: str
    uncached_input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0
    # read / (read + creation); null when the model has no cache activity.
    cache_hit_rate: Optional[float] = None
    severity: str  # "ok" | "info" | "warn"
    recommendation: str


class CacheRecommendationsResponse(BaseModel):
    since: date
    until: date
    # Tenant-wide read / (read + creation) across all models; null if no
    # cache activity in the window.
    overall_hit_rate: Optional[float] = None
    models: list[CacheModelStat]


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def _cache_recommendation(
    uncached_input: int, cache_read: int, cache_creation: int
) -> tuple[str, Optional[float], str]:
    """Pure cache-efficiency verdict for one model over the window.

    Returns ``(severity, hit_rate, recommendation)`` — severity in
    ``{ok, info, warn}``; hit_rate is ``read / (read + creation)`` or
    ``None`` when there's no cache activity. Tested directly so the tiers
    don't depend on DB reachability.
    """
    cache_total = cache_read + cache_creation
    total_input = uncached_input + cache_total
    hit_rate = (cache_read / cache_total) if cache_total > 0 else None

    if total_input < _CACHE_VOLUME_FLOOR:
        return (
            "ok",
            hit_rate,
            "Low input volume — prompt caching has little impact at this "
            "scale yet.",
        )
    if cache_total == 0:
        return (
            "warn",
            None,
            f"No prompt caching detected on {total_input:,} input tokens. "
            "Caching the stable prompt prefix could cut input cost "
            "substantially.",
        )
    assert hit_rate is not None  # cache_total > 0 here
    if hit_rate < 0.5:
        return (
            "warn",
            hit_rate,
            f"Low cache reuse ({_pct(hit_rate)}). Cache is written but "
            "rarely read back — stabilize the cached prefix or raise the "
            "cache TTL so writes are reused before they expire.",
        )
    if hit_rate < 0.8:
        return (
            "info",
            hit_rate,
            f"Moderate cache reuse ({_pct(hit_rate)}). Room to improve — "
            "more of the prompt prefix could be cached and reused.",
        )
    return ("ok", hit_rate, f"Healthy cache reuse ({_pct(hit_rate)}).")


@router.get(
    "/usage/cache-recommendations",
    response_model=CacheRecommendationsResponse,
    operation_id="getCacheRecommendations",
    tags=["usage"],
    summary="Per-model cache hit-rate + prompt-caching recommendations",
)
def get_cache_recommendations(
    since: Optional[date] = Query(None),
    until: Optional[date] = Query(None),
    user: AuthenticatedUser = Depends(current_user),
) -> CacheRecommendationsResponse:
    """Per-model cache hit-rate + a recommendation, computed from the
    captured Admin-API usage records (no new ingest)."""
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "no_tenant_bound",
                "message": "Your session is not bound to a tenant yet.",
            },
        )

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

    params: dict[str, Any] = {
        "since_ts": datetime.combine(since, datetime.min.time()),
        "until_ts": datetime.combine(
            until + timedelta(days=1), datetime.min.time()
        ),
    }

    # Same source + supersession filter as /usage (hide legacy aggregate
    # rows on dates that also have per-model breakdown, so tokens aren't
    # double-counted), then SUM per model.
    sql = """
        WITH expanded AS (
            SELECT tr.occurred_at, r.result
            FROM telemetry_records tr,
                 jsonb_array_elements(tr.metadata->'results')
                     WITH ORDINALITY AS r(result, ordinality)
            WHERE tr.tenant_id = current_setting('app.tenant_id')
              AND tr.record_type = 'usage'
              AND tr.source_api = 'admin'
              AND tr.occurred_at >= :since_ts
              AND tr.occurred_at < :until_ts
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
        )
        SELECT
            COALESCE(result->>'model', '(unspecified)') AS model,
            COALESCE(SUM((result->>'input_tokens')::bigint), 0)
                AS uncached_input,
            COALESCE(SUM((result->>'output_tokens')::bigint), 0)
                AS output_tokens,
            COALESCE(SUM((result->>'cache_read_input_tokens')::bigint), 0)
                AS cache_read,
            COALESCE(SUM(COALESCE(
                NULLIF((result->>'cache_creation_input_tokens')::bigint, 0),
                ((result->'cache_creation')->>'ephemeral_5m_input_tokens')::bigint
                + ((result->'cache_creation')->>'ephemeral_1h_input_tokens')::bigint,
                0
            )), 0) AS cache_creation
        FROM expanded
        GROUP BY 1
    """

    with session_scope(user.tenant_id) as s:
        rows = s.execute(sql_text(sql), params).all()

    models: list[CacheModelStat] = []
    total_read = 0
    total_creation = 0
    for r in rows:
        uncached = int(r.uncached_input)
        read = int(r.cache_read)
        creation = int(r.cache_creation)
        total_read += read
        total_creation += creation
        severity, hit_rate, text = _cache_recommendation(
            uncached, read, creation
        )
        models.append(
            CacheModelStat(
                model=r.model,
                uncached_input_tokens=uncached,
                cache_read_tokens=read,
                cache_creation_tokens=creation,
                output_tokens=int(r.output_tokens),
                cache_hit_rate=(
                    round(hit_rate, 4) if hit_rate is not None else None
                ),
                severity=severity,
                recommendation=text,
            )
        )

    # Most actionable first: warnings, then by input volume desc.
    _sev = {"warn": 0, "info": 1, "ok": 2}
    models.sort(
        key=lambda m: (
            _sev.get(m.severity, 3),
            -(
                m.uncached_input_tokens
                + m.cache_read_tokens
                + m.cache_creation_tokens
            ),
        )
    )

    cache_total = total_read + total_creation
    overall = round(total_read / cache_total, 4) if cache_total > 0 else None

    return CacheRecommendationsResponse(
        since=since, until=until, overall_hit_rate=overall, models=models
    )


__all__ = ["router"]
