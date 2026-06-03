# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Model-mix trend card (TM7).

Compares the tenant's per-model spend share over the trailing 7 days
against the immediately-preceding 7 days and flags a material shift in
the mix — the classic signal being a Sonnet → Opus migration that
silently multiplies per-turn cost.

A model is flagged when, week-over-week, either:

- its share moved by at least 30 percentage points (in either
  direction), or
- it was already spending and its spend tripled (``cur/prior >= 3``),
  or
- it had no prior spend, now spends, and already holds at least a
  30% share (a brand-new model that immediately dominates the mix).

The card is ``advisory`` when anything is flagged (it's a "look at
this" signal, not an over-budget alarm) and ``idle`` otherwise. There
is no model-mix detail page in TM7, so the card carries no CTA.
"""

from __future__ import annotations

from decimal import Decimal

from vargate_telemetry.insights import spend_data
from vargate_telemetry.insights.models import Card, InsightItem, idle_card

CARD_ID = "model_mix"
CARD_TITLE = "Model mix trends"

# Window the mix is compared over: this 7 days vs the prior 7 days.
_WINDOW_DAYS = 7

# Flag thresholds.
_SHARE_SHIFT_PP = Decimal("0.30")  # 30-point share move, either way.
_SPEND_MULTIPLE = 3  # 3x week-over-week spend on an existing model.

# Empty-state copy. Two variants: not enough recent spend to compare at
# all, vs. a stable mix.
_EMPTY_NO_SPEND = (
    "Not enough recent spend to compare model mix. We flag a 30-point "
    "share shift or a 3x spend move week-over-week (e.g. a Sonnet to "
    "Opus migration that multiplies per-turn cost)."
)
_EMPTY_STABLE = (
    "Your model mix is stable vs last week. We flag a 30-point share "
    "shift or a 3x spend move (e.g. a Sonnet to Opus migration that "
    "multiplies per-turn cost)."
)


def _pct(share: Decimal) -> str:
    """Render a 0..1 share as a no-decimal percentage, e.g. ``41%``."""
    return f"{round(share * 100)}%"


def _signed_pp(delta_pp: Decimal) -> str:
    """Render a signed percentage-point delta, e.g. ``+29pp`` / ``-12pp``."""
    return f"{round(delta_pp * 100):+d}pp"


def build_card(tenant_id: str, window: str) -> Card:
    """Build the model-mix trend card for ``tenant_id``.

    ``window`` is accepted for signature parity with the other cards
    but the comparison is always this-7-days vs prior-7-days — a mix
    shift is a week-over-week signal, not a function of the page's
    selected window.
    """
    cur = spend_data.model_share(tenant_id, _WINDOW_DAYS)
    prior = spend_data.model_share(
        tenant_id, _WINDOW_DAYS, offset_days=_WINDOW_DAYS
    )

    # No current spend to speak of → can't compare a mix.
    cur_total = sum((usd for usd, _ in cur.values()), Decimal("0"))
    if not cur or cur_total <= 0:
        return idle_card(
            CARD_ID, CARD_TITLE, empty_state=_EMPTY_NO_SPEND
        )

    items: list[InsightItem] = []
    grew_opus = False

    for model in sorted(cur.keys() | prior.keys()):
        cur_usd, cur_share = cur.get(model, (Decimal("0"), Decimal("0")))
        prior_usd, prior_share = prior.get(
            model, (Decimal("0"), Decimal("0"))
        )
        delta_pp = cur_share - prior_share

        flagged = (
            abs(delta_pp) >= _SHARE_SHIFT_PP
            or (prior_usd > 0 and cur_usd / prior_usd >= _SPEND_MULTIPLE)
            or (
                prior_usd == 0
                and cur_usd > 0
                and cur_share >= _SHARE_SHIFT_PP
            )
        )
        if not flagged:
            continue

        items.append(
            InsightItem(
                label=model,
                detail=f"{_pct(prior_share)} -> {_pct(cur_share)} of spend",
                value=_signed_pp(delta_pp),
            )
        )
        if delta_pp > 0 and "opus" in model.lower():
            grew_opus = True

    if not items:
        return idle_card(
            CARD_ID, CARD_TITLE, empty_state=_EMPTY_STABLE
        )

    if grew_opus:
        # Name the most-grown Opus model in the headline.
        grown_opus = max(
            (
                model
                for model in (it.label for it in items)
                if "opus" in model.lower()
            ),
            key=lambda m: cur.get(m, (Decimal("0"), Decimal("0")))[1]
            - prior.get(m, (Decimal("0"), Decimal("0")))[1],
        )
        headline = (
            f"Model mix shifted toward {grown_opus} this week -- "
            "likely higher per-turn cost. Flag if unintended."
        )
    else:
        n = len(items)
        headline = (
            f"{n} model(s) shifted 30+ points in spend share this week."
        )

    return Card(
        id=CARD_ID,
        title=CARD_TITLE,
        severity="advisory",
        findings_count=len(items),
        headline=headline,
        items=items,
        empty_state=None,
        cta=None,
    )
