# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Workspace cost-attribution card (TM7).

Ranks workspaces by their share of priceable Admin-API spend over the
window and flags concentration when one (or the top two) workspaces
dominate.

Data reality: today most/all tenants' usage records carry
``workspace_id = null`` — the usage connector does not yet request
``group_by=workspace_id`` — so :func:`spend_data.workspace_spend`
usually returns ``[]``. That empty result is the normal idle case
here, not an error: this is honest scaffolding that lights up once
workspace-level usage starts flowing.

Note on the metric: the spec's per-session cost ratio is not cleanly
computable — sessions derive from non-admin ``source_api`` records,
while ``workspace_id`` lives on the admin usage rows — so we rank by
spend share / concentration instead.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from vargate_telemetry.insights import spend_data
from vargate_telemetry.insights.models import Card, InsightItem, idle_card

CARD_ID = "workspace_attribution"
CARD_TITLE = "Workspace cost attribution"

# Concentration threshold: a single workspace at or above this share of
# total spend earns a "dominates" headline.
_CONCENTRATION_THRESHOLD = Decimal("0.60")

# Show at most this many workspaces as line items.
_TOP_N = 4


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


def build_card(tenant_id: str, window: str) -> Card:
    rows = spend_data.workspace_spend(tenant_id, spend_data.window_to_days(window))

    if not rows:
        return idle_card(
            CARD_ID,
            CARD_TITLE,
            empty_state=(
                "Per-workspace cost breakdown needs workspace grouping "
                "enabled on your usage connector. Once workspace-level "
                "usage flows, we will rank workspaces by spend and flag "
                "any whose cost concentration runs high."
            ),
        )

    total = sum((r.usd for r in rows), Decimal("0"))
    top = rows[:_TOP_N]

    items = [
        InsightItem(
            label=r.name or r.workspace_id,
            detail=f"{_pct(r.usd / total)}% of spend",
            value=_money(r.usd),
        )
        for r in top
    ]

    # Concentration headline. ``rows`` is sorted desc by usd, so
    # ``top[0]`` is the heaviest spender.
    top0_share = top[0].usd / total
    top0_name = top[0].name or top[0].workspace_id

    if top0_share >= _CONCENTRATION_THRESHOLD:
        headline = f"{top0_name} accounts for {_pct(top0_share)}% of spend"
    elif len(rows) >= 2:
        second = rows[1]
        second_name = second.name or second.workspace_id
        combined = (top[0].usd + second.usd) / total
        headline = (
            f"{top0_name} and {second_name} account for "
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
        cta=None,  # No per-workspace detail page in TM7.
    )
