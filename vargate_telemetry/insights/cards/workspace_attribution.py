# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Project / workspace cost-attribution card (TM7; cross-vendor TM8 Phase D).

Ranks the tenant's cost centres by their share of priceable spend over
the window and flags concentration when one (or the top two) dominate.
A "cost centre" is **vendor-tagged**:

  - an **Anthropic workspace** (the ``workspaces`` side table, sliced
    from ``result->>'workspace_id'`` on the ``admin`` usage stream), or
  - an **OpenAI project** (the ``openai_projects`` side table, sliced
    from the ``openai_admin_costs`` / ``openai_admin_usage`` streams).

Both vendors merge into ONE ranking; each :class:`InsightItem` carries
the vendor in its ``detail`` so the same card answers "where is my AI
spend going" across both clouds.

Per-vendor cost basis (TM8 conventions, "/usage and /costs are
complementary")
-------------------------------------------------------------------
- **Anthropic** — usage-token **estimate** via :func:`spend_data.workspace_spend`
  (numbers unchanged from TM7; that primitive is byte-for-byte the same).
- **OpenAI** — **authoritative** per-project billed spend from the
  ``openai_admin_costs`` stream (``amount_value``, which includes
  non-token line items) when that stream has rows in the window; else
  the ``openai_admin_usage`` token **estimate**. Same "best source"
  rule the per-vendor spend split uses.

Data reality
------------
Most/all tenants' Anthropic usage records carry ``workspace_id = null``
(the connector does not yet request ``group_by=workspace_id``), and a
tenant with no OpenAI key has no projects at all — so on a typical
single-vendor Personal tenant this card is still ``idle`` with an
honest empty-state. It lights up once either a workspace dimension or
an OpenAI project starts producing priceable spend.

Note on the metric: the spec's per-session cost ratio is not cleanly
computable — sessions derive from non-admin ``source_api`` records,
while ``workspace_id`` / ``project_id`` live on the cost/usage rows —
so we rank by spend share / concentration instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from sqlalchemy import text as sql_text

from vargate_telemetry.db import session_scope
from vargate_telemetry.insights import spend_data
from vargate_telemetry.insights.models import Card, InsightItem, idle_card
from vargate_telemetry.pricing import vendor_cost

CARD_ID = "workspace_attribution"
CARD_TITLE = "Project / workspace attribution"

# Concentration threshold: a single cost centre at or above this share
# of total spend earns a "dominates" headline.
_CONCENTRATION_THRESHOLD = Decimal("0.60")

# Show at most this many cost centres as line items.
_TOP_N = 4

# OpenAI source_api streams this card reads. The usage stream is the
# one ``vendor_cost`` can price; ``openai_admin_costs`` carries the
# authoritative billed amount (read directly, not via the estimator —
# same posture as spend_data).
_SOURCE_OPENAI_USAGE = vendor_cost.SOURCE_API_OPENAI_USAGE
_SOURCE_OPENAI_COSTS = "openai_admin_costs"


@dataclass
class _Entry:
    """One vendor-tagged cost centre in the merged ranking.

    ``label`` is the resolved human name (workspace / project name),
    falling back to the raw id when the side table hasn't seen it yet.
    ``vendor`` is the display vendor (``"Anthropic"`` / ``"OpenAI"``),
    surfaced on the item so a customer can tell two same-named cost
    centres apart across clouds. ``usd`` is the window total.
    """

    label: str
    vendor: str
    usd: Decimal


def _money(amount: Decimal) -> str:
    """Format USD as a no-decimal, thousands-separated dollar amount.

    Truncates toward zero to whole dollars (so ``Decimal("1240.55")``
    -> ``"$1,240"``, matching the spec example) rather than rounding a
    partial dollar up. Built with a placeholder prefix so the literal
    dollar sign never abuts a curly brace.
    """
    whole = int(amount.to_integral_value(rounding=ROUND_DOWN))
    return "$" + "{:,d}".format(whole)


def _pct(value: Decimal) -> int:
    """Whole-percent rendering of a 0..1 share."""
    return int((value * Decimal("100")).to_integral_value())


# ───────────────────────────────────────────────────────────────────────────
# OpenAI per-project spend (lives here, not in spend_data: the TM8
# spend_data additions are fixed to the vendor_spend_breakdown /
# vendor_daily_spend pair, and this card is the only consumer of the
# per-project grain).
# ───────────────────────────────────────────────────────────────────────────
#
# Authoritative path: SUM the billed ``amount_value`` per project from
# the costs stream, resolving the project name via openai_projects (and
# the cost record's own ``project_name``, which the recon notes the
# /costs endpoint uniquely returns). A null project_id is bucketed under
# a stable sentinel so its spend is never silently dropped.

_OPENAI_COSTS_BY_PROJECT_SQL = sql_text(
    """
    SELECT
        COALESCE(tr.metadata->>'project_id', '-') AS project_id,
        COALESCE(
            MAX(p.name),
            MAX(tr.metadata->>'project_name')
        ) AS name,
        COALESCE(SUM((tr.metadata->>'amount_value')::numeric), 0) AS amount
    FROM telemetry_records tr
    LEFT JOIN openai_projects p
      ON p.tenant_id = tr.tenant_id
     AND p.project_id = (tr.metadata->>'project_id')
    WHERE tr.tenant_id = current_setting('app.tenant_id')
      AND tr.record_type = 'cost'
      AND tr.source_api = :source_api
      AND (tr.metadata->>'amount_value') IS NOT NULL
      AND tr.occurred_at
          >= (now() AT TIME ZONE 'UTC') - make_interval(days => :days)
    GROUP BY COALESCE(tr.metadata->>'project_id', '-')
    """
)


def _openai_project_costs(tenant_id: str, days: int) -> list[_Entry]:
    """Authoritative OpenAI per-project spend from ``openai_admin_costs``.

    Returns one :class:`_Entry` per project with non-zero billed spend
    in the window (empty-bucket sentinels carry ``amount_value = null``
    and are filtered out). ``[]`` when the costs stream has no priceable
    rows — the caller then falls back to the usage estimate.
    """
    with session_scope(tenant_id) as s:
        rows = s.execute(
            _OPENAI_COSTS_BY_PROJECT_SQL,
            {"days": days, "source_api": _SOURCE_OPENAI_COSTS},
        ).all()

    entries: list[_Entry] = []
    for row in rows:
        amount = Decimal(str(row.amount))
        if amount <= 0:
            continue
        entries.append(
            _Entry(
                label=row.name or _project_fallback_label(row.project_id),
                vendor=vendor_cost.VENDOR_OPENAI,
                usd=amount,
            )
        )
    return entries


def _openai_project_estimate(tenant_id: str, days: int) -> list[_Entry]:
    """Fallback: usage-token **estimated** OpenAI spend per project.

    Iterates the ``openai_admin_usage`` records, prices each through the
    cross-vendor primitive (double-count-safe), and buckets the cost by
    the record's top-level ``project_id`` (the grouped grain the usage
    pull writes). Used only when the authoritative costs stream is
    empty.
    """
    sql = sql_text(
        """
        SELECT
            COALESCE(tr.metadata->>'project_id', '-') AS project_id,
            tr.occurred_at,
            tr.metadata
        FROM telemetry_records tr
        WHERE tr.tenant_id = current_setting('app.tenant_id')
          AND tr.record_type = 'usage'
          AND tr.source_api = :source_api
          AND tr.occurred_at
              >= (now() AT TIME ZONE 'UTC') - make_interval(days => :days)
        """
    )
    with session_scope(tenant_id) as s:
        rows = s.execute(
            sql, {"days": days, "source_api": _SOURCE_OPENAI_USAGE}
        ).all()

    by_project: dict[str, Decimal] = {}
    for row in rows:
        cost = vendor_cost.estimate_record_cost_usd(
            _SOURCE_OPENAI_USAGE,
            row.metadata or {},
            row.occurred_at,
        )
        if cost is None:
            continue
        by_project[row.project_id] = (
            by_project.get(row.project_id, Decimal("0")) + cost
        )

    if not by_project:
        return []

    # Resolve names for the projects we actually saw spend on.
    names = _resolve_project_names(tenant_id, list(by_project.keys()))
    return [
        _Entry(
            label=names.get(pid) or _project_fallback_label(pid),
            vendor=vendor_cost.VENDOR_OPENAI,
            usd=usd,
        )
        for pid, usd in by_project.items()
    ]


def _resolve_project_names(
    tenant_id: str, project_ids: list[str]
) -> dict[str, Optional[str]]:
    """Map ``project_id -> name`` via ``openai_projects`` for the given ids."""
    real = [p for p in project_ids if p and p != "-"]
    if not real:
        return {}
    sql = sql_text(
        """
        SELECT project_id, name
        FROM openai_projects
        WHERE tenant_id = current_setting('app.tenant_id')
          AND project_id = ANY(:ids)
        """
    )
    with session_scope(tenant_id) as s:
        rows = s.execute(sql, {"ids": real}).all()
    return {row.project_id: row.name for row in rows}


def _project_fallback_label(project_id: Optional[str]) -> str:
    """Display label for a project we couldn't resolve a name for.

    A real-but-unnamed id surfaces as the raw id (matches the Anthropic
    workspace fallback); the null/`-` sentinel becomes a readable
    "Unattributed" so its spend still appears in the ranking rather than
    vanishing or showing a bare dash.
    """
    if not project_id or project_id == "-":
        return "Unattributed project"
    return project_id


def _openai_entries(tenant_id: str, days: int) -> list[_Entry]:
    """OpenAI per-project entries, authoritative-preferred.

    Authoritative ``/costs`` per project when that stream has billed
    rows in the window; otherwise the usage-token estimate. Mirrors the
    "best source" preference of :func:`spend_data.vendor_spend_breakdown`
    — at the per-project grain instead of the vendor total.
    """
    actual = _openai_project_costs(tenant_id, days)
    if actual:
        return actual
    return _openai_project_estimate(tenant_id, days)


def _anthropic_entries(tenant_id: str, days: int) -> list[_Entry]:
    """Anthropic per-workspace entries via the unchanged TM7 primitive."""
    return [
        _Entry(
            label=ws.name or ws.workspace_id,
            vendor=vendor_cost.VENDOR_ANTHROPIC,
            usd=ws.usd,
        )
        for ws in spend_data.workspace_spend(tenant_id, days)
    ]


def build_card(tenant_id: str, window: str) -> Card:
    days = spend_data.window_to_days(window)

    # Merge both vendors into one ranking, each entry vendor-tagged.
    entries = _anthropic_entries(tenant_id, days) + _openai_entries(
        tenant_id, days
    )

    if not entries:
        return idle_card(
            CARD_ID,
            CARD_TITLE,
            empty_state=(
                "Per-project / per-workspace cost breakdown needs either "
                "workspace grouping on your Anthropic usage connector or an "
                "OpenAI project with spend. Once cost-centre data flows, we "
                "will rank projects and workspaces by spend and flag any "
                "whose cost concentration runs high."
            ),
        )

    entries.sort(key=lambda e: e.usd, reverse=True)

    total = sum((e.usd for e in entries), Decimal("0"))
    top = entries[:_TOP_N]

    items = [
        InsightItem(
            label=e.label,
            detail=f"{e.vendor} · {_pct(e.usd / total)}% of spend",
            value=_money(e.usd),
        )
        for e in top
    ]

    # Concentration headline. ``entries`` is sorted desc by usd, so
    # ``top[0]`` is the heaviest spender.
    top0_share = top[0].usd / total
    top0_name = top[0].label

    if top0_share >= _CONCENTRATION_THRESHOLD:
        headline = f"{top0_name} accounts for {_pct(top0_share)}% of spend"
    elif len(entries) >= 2:
        second = entries[1]
        combined = (top[0].usd + second.usd) / total
        headline = (
            f"{top0_name} and {second.label} account for "
            f"{_pct(combined)}% of spend"
        )
    else:
        headline = f"{top0_name} accounts for {_pct(top0_share)}% of spend"

    return Card(
        id=CARD_ID,
        title=CARD_TITLE,
        severity="advisory",
        findings_count=len(items),
        headline=headline,
        items=items,
        empty_state=None,
        cta=None,  # No per-project detail page in TM8.
    )
