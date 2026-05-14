# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Spend computation for a budget's current period (TM3 Phase B).

Both the budgets-detail endpoint AND the alert evaluator read from
this module so the "ratio of current spend to threshold" number
the dashboard renders is the SAME number the evaluator compares
against the 0.70 / 0.85 / 1.00 thresholds. If those two figures
ever drift, customers would see a 71% progress bar but no alert,
or get an alert at "65%" — both worse than no feature at all.

Scope filtering
===============

A budget targets one of four scopes:

- ``tenant``:    sum every admin-API usage record in the period
- ``workspace``: filter ``result->>'workspace_id' = scope_value``
- ``model``:     filter ``result->>'model' = scope_value``
- ``api_key``:   filter ``result->>'api_key_id' = scope_value``

The same supersession filter from ``api.usage`` applies — without
it, pre-TM3 aggregate rows with ``model = null`` would double-count
against the same days as per-model breakdown rows.

Cost is computed per-row in Python via
``pricing.compute_cost_usd``. ``Decimal`` arithmetic is mandatory:
summing a billing cycle in float drifts by cents over a month,
which is the difference between firing the 100% threshold on day
29 vs. day 30.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from vargate_telemetry.pricing import compute_cost_usd

_log = logging.getLogger(__name__)

# Alert thresholds — fire at 70 / 85 / 100 percent of cap.
# Three Decimals to match the NUMERIC(3,2) column type. The
# 0.70/0.85/1.00 values are also enforced by the
# ``ck_budget_alert_events_threshold_values`` CHECK in migration 0019.
ALERT_THRESHOLDS: tuple[Decimal, Decimal, Decimal] = (
    Decimal("0.70"),
    Decimal("0.85"),
    Decimal("1.00"),
)

ScopeKind = Literal["api_key", "workspace", "model", "tenant"]


# Map ``scope_kind`` → the bucket-result JSONB key that holds its
# value. ``tenant`` is the no-filter case so it has no entry here.
_SCOPE_KEY_BY_KIND: dict[ScopeKind, str] = {
    "api_key": "api_key_id",
    "workspace": "workspace_id",
    "model": "model",
}


def compute_spend_in_window(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    scope_kind: ScopeKind,
    scope_value: str | None,
) -> Decimal:
    """Sum estimated cost for admin-API records in [start, end).

    The caller is responsible for binding the session to a tenant
    via ``session_scope(tenant_id)`` — this function trusts the
    RLS predicate that comes from ``app.tenant_id``. Calling
    outside ``session_scope`` will read zero rows.

    Parameters
    ----------
    session:
        SQLAlchemy session bound to a tenant via the standard
        ``session_scope`` path.
    start, end:
        Half-open UTC window. End is exclusive.
    scope_kind:
        One of ``api_key`` / ``workspace`` / ``model`` / ``tenant``.
    scope_value:
        Required if ``scope_kind != "tenant"`` (a budget-row CHECK
        guarantees this; the function additionally raises
        ``ValueError`` if the contract is broken, to catch bugs
        early).

    Returns
    -------
    Decimal sum of cost USD across all matching breakdown rows in
    the window. Rows whose ``model`` is null or unknown contribute
    zero (their ``compute_cost_usd`` returns ``None`` — we never
    fake a number).
    """
    if scope_kind != "tenant" and not scope_value:
        raise ValueError(
            f"scope_kind={scope_kind!r} requires a non-empty scope_value"
        )
    if scope_kind == "tenant" and scope_value is not None:
        raise ValueError(
            "scope_kind='tenant' must have scope_value=None"
        )

    params: dict[str, object] = {
        "start_ts": start,
        "end_ts": end,
    }

    # The result-group filter applies inside the jsonb_array_elements
    # CROSS JOIN — a single record can carry many breakdown rows with
    # different (workspace, key, model), and the budget scope picks
    # one slice of them.
    scope_filter = ""
    if scope_kind != "tenant":
        json_key = _SCOPE_KEY_BY_KIND[scope_kind]
        scope_filter = f"AND (r.result->>'{json_key}') = :scope_value"
        params["scope_value"] = scope_value

    # Supersession filter — identical shape to ``api/usage.py``.
    # Per-model rows (group_by'd response) on a given UTC date hide
    # ALL legacy aggregate rows (model=null) on that same date so
    # the two shapes don't double-count.
    sql = sql_text(
        f"""
        WITH expanded AS (
            SELECT
                tr.id::text AS record_id,
                tr.occurred_at,
                r.result
            FROM telemetry_records tr,
                 jsonb_array_elements(tr.metadata->'results')
                     AS r(result)
            WHERE tr.tenant_id = current_setting('app.tenant_id')
              AND tr.record_type = 'usage'
              AND tr.source_api = 'admin'
              AND tr.occurred_at >= :start_ts
              AND tr.occurred_at <  :end_ts
              {scope_filter}
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
            e.occurred_at,
            e.result->>'model' AS model,
            COALESCE((e.result->>'input_tokens')::bigint, 0) AS input_tokens,
            COALESCE((e.result->>'output_tokens')::bigint, 0) AS output_tokens,
            COALESCE((e.result->>'cache_read_input_tokens')::bigint, 0)
                AS cache_read_tokens,
            -- Mirror api/usage.py's cache_creation handling — see the
            -- NULLIF + nested-ephemeral logic there for the rationale.
            COALESCE(
                NULLIF(
                    (e.result->>'cache_creation_input_tokens')::bigint, 0
                ),
                ((e.result->'cache_creation')->>'ephemeral_5m_input_tokens')::bigint
                + ((e.result->'cache_creation')->>'ephemeral_1h_input_tokens')::bigint,
                0
            ) AS cache_creation_tokens
        FROM expanded e
        """
    )

    total = Decimal("0.00")
    for row in session.execute(sql, params).mappings():
        model = row["model"]
        if not model:
            # Legacy aggregate row that wasn't superseded — we can't
            # price an unknown model, so it contributes zero. The
            # supersession filter already drops aggregates whenever
            # per-model breakdown rows are present for the same day,
            # so this branch is hit only for genuinely-untyped buckets.
            continue
        cost = compute_cost_usd(
            model=model,
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            cache_read_tokens=int(row["cache_read_tokens"]),
            cache_creation_tokens=int(row["cache_creation_tokens"]),
            occurred_at=row["occurred_at"],
        )
        if cost is None:
            continue
        total += cost

    # Quantize to two decimals so callers can store / display
    # without extra precision drift. ``threshold_usd`` is
    # NUMERIC(10,2) so this matches the column shape.
    return total.quantize(Decimal("0.01"))
