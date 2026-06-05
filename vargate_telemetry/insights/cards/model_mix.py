# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Model-mix trend card (TM7, cross-vendor in TM8 Phase D).

Compares the tenant's spend mix over the trailing 7 days against the
immediately-preceding 7 days and flags material shifts. Two kinds of
finding:

1. **Within-vendor model shift** (the original TM7 signal) — a model
   whose share of spend moved materially week-over-week. The classic
   case is a Sonnet → Opus migration that silently multiplies per-turn
   cost; the OpenAI analogue is gpt-4o-mini → gpt-4o. A model is
   flagged when, week-over-week, either:

   - its share moved by at least 30 percentage points (either way), or
   - it was already spending and its spend tripled (``cur/prior >= 3``),
     or
   - it had no prior spend, now spends, and already holds at least a
     30% share (a brand-new model that immediately dominates the mix).

2. **Vendor-mix shift** (TM8 Phase D) — a *vendor* whose share of total
   spend moved by at least 20 percentage points week-over-week, or whose
   absolute spend moved by at least 3× (in either direction). This
   catches a tenant pivoting spend from Anthropic to OpenAI (or back)
   even when no single model trips the within-vendor test.

Both signals are derived from the SAME per-``(vendor, model)`` cost
grouping over each 7-day window, priced through
:func:`vargate_telemetry.pricing.vendor_cost.estimate_record_cost_usd`
(usage-token estimate for both vendors — a coherent basis for share
math, and bit-for-bit identical to TM7 for Anthropic's models). The
authoritative OpenAI ``/costs`` stream is deliberately NOT used here:
mix is a *relative* signal, and mixing an authoritative OpenAI figure
against an estimated Anthropic one in the same ratio would be
apples-to-oranges.

The card is ``advisory`` when anything is flagged (it's a "look at this"
signal, not an over-budget alarm) and ``idle`` otherwise. There is no
model-mix detail page, so the card carries no CTA.
"""

from __future__ import annotations

from datetime import timezone
from decimal import Decimal

from sqlalchemy import text as sql_text

from vargate_telemetry.db import session_scope
from vargate_telemetry.insights.models import Card, InsightItem, idle_card
from vargate_telemetry.pricing.vendor_cost import (
    estimate_record_cost_usd,
    vendor_of,
)

CARD_ID = "model_mix"
CARD_TITLE = "Model mix trends"

# Window the mix is compared over: this 7 days vs the prior 7 days.
_WINDOW_DAYS = 7

# Within-vendor model flag thresholds (unchanged from TM7).
_SHARE_SHIFT_PP = Decimal("0.30")  # 30-point share move, either way.
_SPEND_MULTIPLE = 3  # 3x week-over-week spend on an existing model.

# Vendor-mix flag thresholds (TM8 Phase D). A vendor's share of TOTAL
# spend moving >= 20pp, or its absolute spend moving >= 3x, either way.
_VENDOR_SHARE_SHIFT_PP = Decimal("0.20")
_VENDOR_SPEND_MULTIPLE = Decimal("3")

# The two usage streams the mix reads, one per vendor. (Authoritative
# OpenAI /costs is intentionally excluded — see the module docstring.)
_USAGE_SOURCES = ("admin", "openai_admin_usage")

# Empty-state copy. Two variants: not enough recent spend to compare at
# all, vs. a stable mix.
_EMPTY_NO_SPEND = (
    "Not enough recent spend to compare model mix. We flag a 30-point "
    "model-share shift, a 3x model-spend move, or a 20-point shift in "
    "the mix between vendors week-over-week (e.g. a Sonnet to Opus "
    "migration, or spend pivoting from Anthropic to OpenAI)."
)
_EMPTY_STABLE = (
    "Your model and vendor mix are stable vs last week. We flag a "
    "30-point model-share shift, a 3x model-spend move, or a 20-point "
    "shift in the mix between vendors (e.g. a Sonnet to Opus migration "
    "that multiplies per-turn cost)."
)


def _pct(share: Decimal) -> str:
    """Render a 0..1 share as a no-decimal percentage, e.g. ``41%``."""
    return f"{round(share * 100)}%"


def _signed_pp(delta_pp: Decimal) -> str:
    """Render a signed percentage-point delta, e.g. ``+29pp`` / ``-12pp``."""
    return f"{round(delta_pp * 100):+d}pp"


def _vendor_model_costs(
    tenant_id: str, days: int, offset_days: int = 0
) -> dict[tuple[str, str], Decimal]:
    """Per-``(vendor, model)`` USD over a trailing window.

    Window ``[now - offset_days - days, now - offset_days)`` in UTC (so a
    caller passes ``offset_days=days`` to read the immediately-preceding
    period). Reads BOTH usage streams (``admin`` + ``openai_admin_usage``)
    and prices each record via
    :func:`vendor_cost.estimate_record_cost_usd`, which dispatches on
    ``source_api`` and applies the OpenAI double-count-safe token split.

    A record that prices to ``None`` (null/unknown model, empty-bucket
    sentinel) contributes nothing; its ``(vendor, model)`` key never
    appears. The Anthropic figures equal the TM7 ``model_share`` path's
    (``estimate_record_cost_usd("admin", ...)`` reproduces the SQL
    pricing exactly), so the within-vendor signal is regression-safe.
    """
    sql = sql_text(
        """
        SELECT tr.source_api, tr.occurred_at, tr.metadata
        FROM telemetry_records tr
        WHERE tr.tenant_id = current_setting('app.tenant_id')
          AND tr.record_type = 'usage'
          AND tr.source_api = ANY(:sources)
          AND tr.occurred_at >= (now() AT TIME ZONE 'UTC')
              - make_interval(days => :offset_days + :days)
          AND tr.occurred_at <  (now() AT TIME ZONE 'UTC')
              - make_interval(days => :offset_days)
        """
    )

    costs: dict[tuple[str, str], Decimal] = {}
    with session_scope(tenant_id) as s:
        rows = s.execute(
            sql,
            {
                "sources": list(_USAGE_SOURCES),
                "days": days,
                "offset_days": offset_days,
            },
        ).all()

    for row in rows:
        occurred = row.occurred_at
        if occurred is None:
            continue
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        cost = estimate_record_cost_usd(
            row.source_api, row.metadata or {}, occurred
        )
        if cost is None:
            continue
        model = _model_of(row.source_api, row.metadata or {})
        if not model:
            continue
        key = (vendor_of(row.source_api), model)
        costs[key] = costs.get(key, Decimal("0")) + cost

    return costs


def _model_of(source_api: str, metadata: dict) -> str | None:
    """Extract the model string from a usage record's metadata.

    Anthropic usage carries one breakdown per record under
    ``metadata['results'][0]['model']`` (post-T5.5.6 one breakdown per
    record); OpenAI under ``metadata['result']['model']``. Returns
    ``None`` when absent so the caller can skip the record.
    """
    if source_api == "openai_admin_usage":
        result = metadata.get("result")
        if isinstance(result, dict):
            return result.get("model")
        return None
    # Anthropic admin (and any future results[]-shaped stream).
    results = metadata.get("results")
    if isinstance(results, list):
        for breakdown in results:
            if isinstance(breakdown, dict) and breakdown.get("model"):
                return breakdown.get("model")
    return None


def _shares(
    costs: dict[tuple[str, str], Decimal],
) -> tuple[dict[str, Decimal], dict[str, Decimal], dict[str, Decimal]]:
    """Derive per-model and per-vendor USD + shares from a cost grouping.

    Returns ``(model_share, vendor_usd, vendor_share)`` where:
      - ``model_share`` maps the display ``"vendor / model"`` key →
        fraction of total spend (0..1),
      - ``vendor_usd`` maps vendor → its absolute USD,
      - ``vendor_share`` maps vendor → fraction of total spend (0..1).

    Empty input (or zero total) → three empty dicts.
    """
    total = sum(costs.values(), Decimal("0"))
    if total <= 0:
        return {}, {}, {}

    model_share: dict[str, Decimal] = {}
    vendor_usd: dict[str, Decimal] = {}
    for (vendor, model), usd in costs.items():
        label = f"{vendor} / {model}"
        model_share[label] = usd / total
        vendor_usd[vendor] = vendor_usd.get(vendor, Decimal("0")) + usd

    vendor_share = {v: usd / total for v, usd in vendor_usd.items()}
    return model_share, vendor_usd, vendor_share


def build_card(tenant_id: str, window: str) -> Card:
    """Build the model-mix trend card for ``tenant_id``.

    ``window`` is accepted for signature parity with the other cards
    but the comparison is always this-7-days vs prior-7-days — a mix
    shift is a week-over-week signal, not a function of the page's
    selected window.
    """
    cur_costs = _vendor_model_costs(tenant_id, _WINDOW_DAYS)
    prior_costs = _vendor_model_costs(
        tenant_id, _WINDOW_DAYS, offset_days=_WINDOW_DAYS
    )

    # No current spend to speak of → can't compare a mix.
    cur_total = sum(cur_costs.values(), Decimal("0"))
    if not cur_costs or cur_total <= 0:
        return idle_card(CARD_ID, CARD_TITLE, empty_state=_EMPTY_NO_SPEND)

    cur_model_share, cur_vendor_usd, cur_vendor_share = _shares(cur_costs)
    prior_model_share, prior_vendor_usd, prior_vendor_share = _shares(
        prior_costs
    )

    items: list[InsightItem] = []
    grew_opus = False

    # ── Vendor-mix shift (TM8 Phase D) — listed first; it's the broader
    # signal. A vendor whose SHARE moved >= 20pp or whose ABSOLUTE spend
    # moved >= 3x, either direction.
    vendor_shift_count = 0
    for vendor in sorted(cur_vendor_share.keys() | prior_vendor_share.keys()):
        cur_v_share = cur_vendor_share.get(vendor, Decimal("0"))
        prior_v_share = prior_vendor_share.get(vendor, Decimal("0"))
        cur_v_usd = cur_vendor_usd.get(vendor, Decimal("0"))
        prior_v_usd = prior_vendor_usd.get(vendor, Decimal("0"))
        delta_pp = cur_v_share - prior_v_share

        flagged = (
            abs(delta_pp) >= _VENDOR_SHARE_SHIFT_PP
            or (
                prior_v_usd > 0
                and cur_v_usd / prior_v_usd >= _VENDOR_SPEND_MULTIPLE
            )
            or (
                prior_v_usd == 0
                and cur_v_usd > 0
                and cur_v_share >= _VENDOR_SHARE_SHIFT_PP
            )
        )
        if not flagged:
            continue
        vendor_shift_count += 1
        items.append(
            InsightItem(
                label=f"{vendor} (vendor)",
                detail=(
                    f"{_pct(prior_v_share)} -> {_pct(cur_v_share)} "
                    "of total spend"
                ),
                value=_signed_pp(delta_pp),
            )
        )

    # ── Within-vendor model shift (the original TM7 signal), now over
    # both vendors' models keyed "vendor / model".
    for model_label in sorted(
        cur_model_share.keys() | prior_model_share.keys()
    ):
        cur_share = cur_model_share.get(model_label, Decimal("0"))
        prior_share = prior_model_share.get(model_label, Decimal("0"))
        # Absolute USD for the 3x test — recover from the cost groupings.
        cur_usd = _usd_for_label(cur_costs, model_label)
        prior_usd = _usd_for_label(prior_costs, model_label)
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
                label=model_label,
                detail=f"{_pct(prior_share)} -> {_pct(cur_share)} of spend",
                value=_signed_pp(delta_pp),
            )
        )
        if delta_pp > 0 and "opus" in model_label.lower():
            grew_opus = True

    if not items:
        return idle_card(CARD_ID, CARD_TITLE, empty_state=_EMPTY_STABLE)

    headline = _headline(
        items, grew_opus, vendor_shift_count, cur_model_share, prior_model_share
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


def _usd_for_label(
    costs: dict[tuple[str, str], Decimal], model_label: str
) -> Decimal:
    """Absolute USD for a ``"vendor / model"`` label from a cost grouping."""
    vendor, _, model = model_label.partition(" / ")
    return costs.get((vendor, model), Decimal("0"))


def _headline(
    items: list[InsightItem],
    grew_opus: bool,
    vendor_shift_count: int,
    cur_model_share: dict[str, Decimal],
    prior_model_share: dict[str, Decimal],
) -> str:
    """Pick the card headline, preferring the loudest concrete signal.

    Order: an Opus migration (the canonical cost-multiplier) →
    a vendor-mix pivot → a generic count of model-share shifts.
    """
    if grew_opus:
        grown_opus = max(
            (
                label
                for label in (it.label for it in items)
                if "opus" in label.lower()
            ),
            key=lambda m: cur_model_share.get(m, Decimal("0"))
            - prior_model_share.get(m, Decimal("0")),
        )
        return (
            f"Model mix shifted toward {grown_opus} this week -- "
            "likely higher per-turn cost. Flag if unintended."
        )
    if vendor_shift_count > 0:
        unit = "vendor" if vendor_shift_count == 1 else "vendors"
        return (
            f"Spend mix shifted across {vendor_shift_count} {unit} this "
            "week. Flag if unintended."
        )
    n = len(items)
    return f"{n} model(s) shifted 30+ points in spend share this week."
