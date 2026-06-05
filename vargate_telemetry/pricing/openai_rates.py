# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""OpenAI API per-model rate cards (TM8 Phase B).

Mirrors :mod:`vargate_telemetry.pricing.anthropic_rates` exactly —
same :class:`ModelRates` / :class:`RateCardEntry` / ``RATE_HISTORY``
shapes, same ``rates_at()`` / ``compute_cost_usd()`` semantics, same
longest-prefix model match, same ``None``-for-unknown discipline.
OpenAI is Ogma's first non-Anthropic vendor; keeping the two rate
modules structurally identical lets the cross-vendor cost path treat
them interchangeably.

Pricing is **versioned by date range** (see ``RATE_HISTORY``):
historical records must compute against the rate active **when
``occurred_at`` happened**, not today's rate. OpenAI has reshuffled
its lineup repeatedly; when a published rate next changes, add a new
:class:`RateCardEntry` and freeze the OLD rates in the prior entry —
DO NOT mutate an existing entry.

Units
-----

All rates are USD **per million tokens** (OpenAI's published unit).
Token counts arrive as ``int``; the helper divides by 1_000_000
before multiplying. :class:`Decimal` end-to-end to avoid float drift
on aggregate sums.

Cache pricing — the OpenAI difference
--------------------------------------

OpenAI prompt caching is **automatic, input-only, ~50% off, and has
NO cache-creation (cache-write) charge** — unlike Anthropic, which
bills a premium to *write* the cache. Consequences for this module:

  - ``cache_creation_per_mtok`` is **always 0** for every OpenAI
    model. The field exists only to keep the :class:`ModelRates`
    shape identical to Anthropic's; OpenAI callers pass
    ``cache_creation_tokens=0`` and it contributes nothing to cost.
  - ``cache_read_per_mtok`` is the discounted (cached-input) rate.
    OpenAI callers map the usage row's ``input_cached_tokens`` to
    ``cache_read_tokens`` and ``input_uncached_tokens`` to
    ``input_tokens`` — never the raw ``input_tokens`` field (which
    is the TOTAL and would double-count the cached portion). See the
    §2.1 double-count trap in ``docs/sprints/TM8-openai-recon.md``.
  - Models without a published cached-input price (legacy chat
    models like gpt-4-turbo / gpt-3.5-turbo, and the embedding
    models) carry ``cache_read_per_mtok == input_per_mtok`` so a
    stray ``input_cached_tokens`` (which OpenAI won't actually emit
    for them) bills at the normal rate rather than free.
  - Embedding models have **no output billing** —
    ``output_per_mtok == 0``. They're input-only.

Source of truth
---------------

Rates per 1M tokens. The **gpt-4o** and **gpt-4o-mini** numbers are
**confirmed against this org's live ``/costs`` data** (TM8 recon) —
they are the anchor. The remaining models are sourced from OpenAI's
published API pricing (https://openai.com/api/pricing/ and
https://developers.openai.com/api/docs/pricing, retrieved
2026-06-05) and cross-checked against pricepertoken.com. Where
sources genuinely diverged a ``TODO`` flags it inline rather than
silently guessing. Update this module when OpenAI bumps a rate; add
a new :class:`RateCardEntry` — never mutate an existing one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional


# ───────────────────────────────────────────────────────────────────────────
# Per-model rate shape  (identical to anthropic_rates.ModelRates)
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelRates:
    """USD per million tokens for one model under one rate card.

    All four fields exist so the shape matches Anthropic's and the
    cross-vendor cost path is uniform. For OpenAI:
      - ``cache_creation_per_mtok`` is always 0 (no cache-write charge);
      - ``cache_read_per_mtok`` is the discounted cached-input rate
        (or == ``input_per_mtok`` for models with no caching);
      - ``output_per_mtok`` is 0 for embedding models (input-only).
    """

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal
    cache_creation_per_mtok: Decimal


@dataclass(frozen=True)
class RateCardEntry:
    """One slice of pricing history.

    ``effective_from`` inclusive; ``effective_to`` exclusive (None =
    open-ended, the current rate). ``rates`` is keyed by OpenAI's
    model string and matched exact-OR-longest-prefix in ``rates_at``
    (see ``_lookup_model``).
    """

    effective_from: datetime
    effective_to: Optional[datetime]
    rates: dict[str, ModelRates]


# ───────────────────────────────────────────────────────────────────────────
# Rate history
# ───────────────────────────────────────────────────────────────────────────
#
# OpenAI's API returns date-stamped model strings
# (``gpt-4o-2024-08-06``, ``o4-mini-2025-04-16``). We key the rate
# dict by the **family** form (``gpt-4o``, ``o4-mini``); the
# longest-prefix fallback in ``_lookup_model`` maps the date-stamped
# variant to the family rate, exactly like anthropic_rates. Unknown
# families fall through to ``None`` — never fake a rate.

# The 2026-current rate card. All cache_creation = 0 (OpenAI has no
# cache-write charge). cache_read is the published cached-input rate
# (== input where the model has no caching).
_RATES_2026: dict[str, ModelRates] = {
    # ── GPT-4o family ──  (ANCHOR — confirmed vs live /costs, TM8 recon)
    "gpt-4o-mini": ModelRates(  # longer prefix than "gpt-4o" — list order
        input_per_mtok=Decimal("0.15"),  # is irrelevant; _lookup_model picks
        output_per_mtok=Decimal("0.60"),  # the longest matching key.
        cache_read_per_mtok=Decimal("0.075"),  # ~50% of input (auto cache)
        cache_creation_per_mtok=Decimal("0"),
    ),
    "gpt-4o": ModelRates(
        input_per_mtok=Decimal("2.50"),
        output_per_mtok=Decimal("10.00"),
        cache_read_per_mtok=Decimal("1.25"),  # confirmed cached rate
        cache_creation_per_mtok=Decimal("0"),
    ),
    # ── GPT-4.1 family ──
    "gpt-4.1-mini": ModelRates(
        input_per_mtok=Decimal("0.40"),
        output_per_mtok=Decimal("1.60"),
        cache_read_per_mtok=Decimal("0.10"),
        cache_creation_per_mtok=Decimal("0"),
    ),
    "gpt-4.1": ModelRates(
        input_per_mtok=Decimal("2.00"),
        output_per_mtok=Decimal("8.00"),
        cache_read_per_mtok=Decimal("0.50"),
        cache_creation_per_mtok=Decimal("0"),
    ),
    # ── Legacy chat models (no prompt caching → cache_read == input) ──
    "gpt-4-turbo": ModelRates(
        input_per_mtok=Decimal("5.00"),
        output_per_mtok=Decimal("15.00"),
        # gpt-4-turbo predates automatic prompt caching; OpenAI won't
        # emit input_cached_tokens for it. cache_read == input so a
        # stray cached count bills normally rather than free.
        cache_read_per_mtok=Decimal("5.00"),
        cache_creation_per_mtok=Decimal("0"),
    ),
    "gpt-3.5-turbo": ModelRates(
        input_per_mtok=Decimal("0.50"),
        # TODO(TM8): sources diverge on gpt-3.5-turbo OUTPUT — OpenAI's
        # current page and pricepertoken.com show $1.50, some
        # aggregators still list the older $1.00. Using $1.50 (the
        # newer/published figure). gpt-3.5-turbo is near-EOL and rarely
        # appears in usage; verify against a real /costs line item if a
        # customer actually runs it.
        output_per_mtok=Decimal("1.50"),
        cache_read_per_mtok=Decimal("0.50"),  # no caching → == input
        cache_creation_per_mtok=Decimal("0"),
    ),
    # ── o-series reasoning models ──
    "o1": ModelRates(
        input_per_mtok=Decimal("15.00"),
        output_per_mtok=Decimal("60.00"),
        cache_read_per_mtok=Decimal("7.50"),
        cache_creation_per_mtok=Decimal("0"),
    ),
    "o3": ModelRates(
        input_per_mtok=Decimal("2.00"),
        output_per_mtok=Decimal("8.00"),
        cache_read_per_mtok=Decimal("0.50"),
        cache_creation_per_mtok=Decimal("0"),
    ),
    "o4-mini": ModelRates(
        # TODO(TM8): one OpenAI docs view showed a date-stamped
        # o4-mini-2025-04-16 at $4.00/$16.00; the majority of sources
        # (openai.com pricing, pricepertoken) and the headline o4-mini
        # SKU are $0.55 in / $2.20 out / $0.275 cached. Using the
        # majority figure as the family rate; if a real /costs line
        # shows the $4 variant, add it as its own date-stamped key.
        input_per_mtok=Decimal("0.55"),
        output_per_mtok=Decimal("2.20"),
        cache_read_per_mtok=Decimal("0.275"),
        cache_creation_per_mtok=Decimal("0"),
    ),
    # ── Embedding models (INPUT-ONLY → output 0; no caching) ──
    "text-embedding-3-small": ModelRates(
        input_per_mtok=Decimal("0.02"),
        output_per_mtok=Decimal("0"),  # embeddings have no output billing
        cache_read_per_mtok=Decimal("0.02"),  # no caching → == input
        cache_creation_per_mtok=Decimal("0"),
    ),
    "text-embedding-3-large": ModelRates(
        input_per_mtok=Decimal("0.13"),
        output_per_mtok=Decimal("0"),
        cache_read_per_mtok=Decimal("0.13"),
        cache_creation_per_mtok=Decimal("0"),
    ),
}


RATE_HISTORY: list[RateCardEntry] = [
    # Single open-ended entry for now. OpenAI's published rates for the
    # models we cover have been stable through the TM8 window; the first
    # real rate change adds a SECOND entry with these rates frozen as the
    # prior (closed) window. effective_from is set well before any record
    # we'd ingest so a backfill never falls below history (rates_at is
    # defensive anyway).
    RateCardEntry(
        effective_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
        effective_to=None,
        rates=_RATES_2026,
    ),
]


CURRENT_RATES: dict[str, ModelRates] = RATE_HISTORY[-1].rates


# ───────────────────────────────────────────────────────────────────────────
# Helpers  (identical semantics to anthropic_rates)
# ───────────────────────────────────────────────────────────────────────────


def rates_at(occurred_at: datetime) -> dict[str, ModelRates]:
    """Return the rate map active at ``occurred_at``.

    Walks ``RATE_HISTORY`` newest-first and returns the first entry
    whose ``[effective_from, effective_to)`` window covers the
    timestamp. Defensive: if ``occurred_at`` precedes the oldest
    entry, returns the oldest entry's rates anyway — better than
    crashing, and historical accuracy beyond our coverage window
    isn't a billing concern.
    """
    if occurred_at.tzinfo is None:
        # Treat naive as UTC — the ingest path always stores UTC, but
        # callers handling raw datetimes might forget the tz.
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
    """Look up a model's rates with a longest-prefix fallback.

    Exact match wins. If exact fails, try the longest key in the rate
    map that the model name starts with — covers OpenAI's date-stamped
    variants: ``gpt-4o-2024-08-06`` → ``gpt-4o``, ``o4-mini-2025-04-16``
    → ``o4-mini``. Longest-prefix (not first-match) is essential here:
    ``gpt-4o-mini`` must NOT be swallowed by the shorter ``gpt-4o`` key,
    and ``gpt-4.1-mini`` must not be swallowed by ``gpt-4.1``.

    Returns ``None`` for unknown families. **Never fakes a rate.**
    """
    exact = rates.get(model)
    if exact is not None:
        return exact

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
    """Cost in USD for one usage row's token totals.

    Keyword signature is **identical** to
    ``anthropic_rates.compute_cost_usd`` so the cross-vendor cost path
    can call either. OpenAI callers map the usage row as:
      - ``input_tokens``          = ``input_uncached_tokens``  (full rate)
      - ``cache_read_tokens``     = ``input_cached_tokens``    (cached rate)
      - ``cache_creation_tokens`` = 0  (OpenAI has no cache-write charge)
      - ``output_tokens``         = ``output_tokens``
    Passing the raw ``input_tokens`` usage field as ``input_tokens``
    here would double-count the cached portion — see the §2.1 trap.

    Returns ``None`` when:
      - ``model`` is ``None``;
      - the model name is unknown to the rate card (surface the gap,
        don't fake a number).

    Returns a :class:`Decimal` rounded to six decimal places — same as
    Anthropic. Aggregate the per-row Decimals and round the TOTAL to
    two decimals; rounding each row independently would compound error.

    Note: because every OpenAI ``cache_creation_per_mtok`` is 0, the
    ``cache_creation_tokens`` term contributes nothing even if a caller
    passes a non-zero count — the OpenAI no-cache-write rule holds by
    construction, not just by caller convention.
    """
    if model is None:
        return None

    rates = _lookup_model(model, rates_at(occurred_at))
    if rates is None:
        return None

    def to_dec(n: int) -> Decimal:
        return Decimal(n)

    million = Decimal(1_000_000)

    cost = (
        rates.input_per_mtok * to_dec(input_tokens)
        + rates.output_per_mtok * to_dec(output_tokens)
        + rates.cache_read_per_mtok * to_dec(cache_read_tokens)
        + rates.cache_creation_per_mtok * to_dec(cache_creation_tokens)
    ) / million

    return cost.quantize(Decimal("0.000001"))
