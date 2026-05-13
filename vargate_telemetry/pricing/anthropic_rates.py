# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Anthropic API per-model rate cards (T5.5.6).

Pricing is **versioned by date range**: rates change (Anthropic
bumped Opus 4.x twice in 2025; Sonnet 4.5 and 4.6 have different
rates from 4.x) and historical records must compute against the
rate that was active **when ``occurred_at`` happened**, not against
today's rate.

Structure
---------

``RATE_HISTORY`` is an ordered list of
``(effective_from, effective_to, rates_dict)`` entries. ``rates_dict``
maps the Anthropic ``model`` string (e.g.
``"claude-sonnet-4-5-20250929"``) to a :class:`ModelRates` instance.
The list is ordered oldest-to-newest; ``rates_at(occurred_at)``
walks newest-first and returns the first entry whose window covers
the timestamp.

``CURRENT_RATES`` is the most recent entry's rate map — useful when
the caller is computing a *projection* (e.g., next-30-days estimate)
rather than billing against a historical timestamp.

Units
-----

All rates are USD **per million tokens** (Anthropic's published
unit). Token counts come in as ``int`` (raw counts); the helper
divides by 1_000_000 before multiplying. :class:`Decimal` end-to-end
to avoid float drift on aggregate sums.

Cache pricing
-------------

Anthropic's prompt-cache pricing has nuance — read and write tiers,
TTL variants (ephemeral_5m vs ephemeral_1h). For T5.5.6 we collapse
to a single read rate and a single write (creation) rate per model.
The ephemeral_5m / ephemeral_1h distinction is a refinement for a
later sprint if customers ask.

Source of truth
---------------

Rates copied from https://docs.claude.com/en/docs/about-claude/models
(retrieved 2026-05-13). Update this module when Anthropic bumps a
rate; add a new :class:`RateCardEntry` row to ``RATE_HISTORY``,
DO NOT mutate an existing entry (historical records must still
compute against their then-current rate).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional


# ───────────────────────────────────────────────────────────────────────────
# Per-model rate shape
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelRates:
    """USD per million tokens for one model under one rate card.

    All four fields are required because Anthropic bills cache read
    and cache creation at distinct (lower) rates from fresh input;
    leaving them at the input rate would overstate cost.
    """

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal
    cache_creation_per_mtok: Decimal


@dataclass(frozen=True)
class RateCardEntry:
    """One slice of pricing history.

    ``effective_from`` inclusive; ``effective_to`` exclusive (None =
    open-ended, the current rate). ``rates`` is keyed by Anthropic's
    model string and matched exact-OR-prefix in ``rates_at`` (see
    ``_lookup_model`` for the prefix-match rule).
    """

    effective_from: datetime
    effective_to: Optional[datetime]
    rates: dict[str, ModelRates]


# ───────────────────────────────────────────────────────────────────────────
# Rate history
# ───────────────────────────────────────────────────────────────────────────
#
# Anthropic's API returns model strings with date-stamped suffixes:
# ``claude-sonnet-4-5-20250929``, ``claude-opus-4-7-20260301``, etc.
# We key the rate dict by the date-stamped form when known; the
# prefix-match fallback in ``_lookup_model`` covers the case where
# the API returns a new date-stamped variant before this table has
# been updated (e.g. claude-sonnet-4-5-20260601 maps to claude-sonnet-4-5's
# rate). Unknown families fall through to ``None``.

# The 2026-05 rate card (Opus 4.7 launched 2026-03-01; Sonnet 4.6
# launched 2026-04-15). Sonnet 4.5 / Opus 4.x kept their rates.
_RATES_2026_05 = {
    # ── Sonnet family ──
    "claude-sonnet-4-5-20250929": ModelRates(
        input_per_mtok=Decimal("3.00"),
        output_per_mtok=Decimal("15.00"),
        cache_read_per_mtok=Decimal("0.30"),
        cache_creation_per_mtok=Decimal("3.75"),
    ),
    "claude-sonnet-4-6": ModelRates(
        input_per_mtok=Decimal("3.00"),
        output_per_mtok=Decimal("15.00"),
        cache_read_per_mtok=Decimal("0.30"),
        cache_creation_per_mtok=Decimal("3.75"),
    ),
    # ── Opus family ──
    "claude-opus-4-7": ModelRates(
        input_per_mtok=Decimal("15.00"),
        output_per_mtok=Decimal("75.00"),
        cache_read_per_mtok=Decimal("1.50"),
        cache_creation_per_mtok=Decimal("18.75"),
    ),
    "claude-opus-4-5": ModelRates(
        input_per_mtok=Decimal("15.00"),
        output_per_mtok=Decimal("75.00"),
        cache_read_per_mtok=Decimal("1.50"),
        cache_creation_per_mtok=Decimal("18.75"),
    ),
    "claude-opus-4-1": ModelRates(
        input_per_mtok=Decimal("15.00"),
        output_per_mtok=Decimal("75.00"),
        cache_read_per_mtok=Decimal("1.50"),
        cache_creation_per_mtok=Decimal("18.75"),
    ),
    # ── Haiku family ──
    "claude-haiku-4-5": ModelRates(
        input_per_mtok=Decimal("1.00"),
        output_per_mtok=Decimal("5.00"),
        cache_read_per_mtok=Decimal("0.10"),
        cache_creation_per_mtok=Decimal("1.25"),
    ),
    "claude-haiku-3-5": ModelRates(
        input_per_mtok=Decimal("0.80"),
        output_per_mtok=Decimal("4.00"),
        cache_read_per_mtok=Decimal("0.08"),
        cache_creation_per_mtok=Decimal("1.00"),
    ),
}


# The 2025-Q3 rate card — kept for historical records ingested
# before Opus 4.7's launch (2026-03-01). Opus 4.5 launched
# 2025-08-15 at the same rate as 4.1, so this card is essentially
# the same as the current one minus 4.7 — but historical records
# from before 2025-08-15 would have hit Opus 4.1, and we want
# rates_at(2025-09-01) to find 4.1 even though it's still in
# CURRENT_RATES (because backwards-compat).
#
# This entry is mostly a placeholder for the pattern; the real
# inflection point is when Anthropic next BUMPS a rate. We add the
# new entry then, with the OLD rates frozen in the prior entry.
_RATES_2025_PRELAUNCH = dict(_RATES_2026_05)
# Drop models that didn't exist before 2026-03 so a record from
# before then can't accidentally match a future model name.
_RATES_2025_PRELAUNCH.pop("claude-opus-4-7", None)


RATE_HISTORY: list[RateCardEntry] = [
    RateCardEntry(
        effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
        effective_to=datetime(2026, 3, 1, tzinfo=timezone.utc),
        rates=_RATES_2025_PRELAUNCH,
    ),
    RateCardEntry(
        effective_from=datetime(2026, 3, 1, tzinfo=timezone.utc),
        effective_to=None,
        rates=_RATES_2026_05,
    ),
]


CURRENT_RATES: dict[str, ModelRates] = RATE_HISTORY[-1].rates


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def rates_at(occurred_at: datetime) -> dict[str, ModelRates]:
    """Return the rate map active at ``occurred_at``.

    Walks ``RATE_HISTORY`` newest-first and returns the first entry
    whose ``[effective_from, effective_to)`` window covers the
    timestamp. Defensive: if ``occurred_at`` precedes the oldest
    entry, returns the oldest entry's rates anyway — better than
    crashing, and historical accuracy beyond our coverage window
    isn't a billing concern (no customer is using a 5-year-old
    record for current spend).
    """
    if occurred_at.tzinfo is None:
        # Treat naive as UTC — the ingest path always stores UTC,
        # but callers handling raw datetimes might forget the tz.
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)

    for entry in reversed(RATE_HISTORY):
        if occurred_at < entry.effective_from:
            continue
        if entry.effective_to is None or occurred_at < entry.effective_to:
            return entry.rates

    # Older than the oldest entry — fall through to the oldest.
    return RATE_HISTORY[0].rates


def _lookup_model(
    model: str, rates: dict[str, ModelRates]
) -> Optional[ModelRates]:
    """Look up a model's rates with a prefix-match fallback.

    Exact match wins. If exact fails, try the longest prefix in the
    rate map that the model name starts with — covers the case where
    Anthropic ships a new date-stamped variant
    (``claude-sonnet-4-5-20260601``) before this table is updated:
    it falls through to ``claude-sonnet-4-5``.

    Returns ``None`` for unknown families. **Never fakes a rate.**
    """
    exact = rates.get(model)
    if exact is not None:
        return exact

    # Longest prefix match. Sort keys by length descending so
    # ``claude-sonnet-4-5-20250929`` beats ``claude-sonnet-4-5`` for
    # the input ``claude-sonnet-4-5-20250929-experimental`` (which
    # would match both prefixes, but the more specific one is right).
    best: Optional[ModelRates] = None
    best_len = 0
    for key, val in rates.items():
        if model.startswith(key) and len(key) > best_len:
            best = val
            best_len = len(key)
    return best


def compute_cost_usd(
    model: Optional[str],
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    occurred_at: datetime,
) -> Optional[Decimal]:
    """Cost in USD for one breakdown row's token totals.

    Returns ``None`` when:
      - ``model`` is ``None`` (legacy aggregate row from pre-T5.5.6
        ingest, or a current-shape row Anthropic emitted with
        model=null);
      - the model name is unknown to the rate card (a never-seen-
        before model family — surface the gap, don't fake a number).

    Returns a :class:`Decimal` with cent-level precision rounded to
    six decimal places. Aggregating thousands of these and rounding
    the total to two decimals is the right pattern — rounding each
    row independently would compound rounding error.
    """
    if model is None:
        return None

    rates = _lookup_model(model, rates_at(occurred_at))
    if rates is None:
        return None

    # Token counts come in as ints; cast to Decimal for the math.
    def to_dec(n: int) -> Decimal:
        return Decimal(n)

    million = Decimal(1_000_000)

    cost = (
        rates.input_per_mtok * to_dec(input_tokens)
        + rates.output_per_mtok * to_dec(output_tokens)
        + rates.cache_read_per_mtok * to_dec(cache_read_tokens)
        + rates.cache_creation_per_mtok * to_dec(cache_creation_tokens)
    ) / million

    # Round to six decimal places — preserves sub-cent precision so
    # totals across thousands of rows don't drift, while keeping
    # JSON-serializable numbers compact.
    return cost.quantize(Decimal("0.000001"))
