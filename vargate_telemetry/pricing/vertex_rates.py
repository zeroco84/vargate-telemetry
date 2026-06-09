# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Google Vertex AI (Gemini) per-model rate cards (TM9 Phase B scaffold).

Mirrors :mod:`vargate_telemetry.pricing.openai_rates` (which in turn
mirrors :mod:`vargate_telemetry.pricing.anthropic_rates`) as closely as
the vendor allows — same :class:`ModelRates` / :class:`RateCardEntry` /
``RATE_HISTORY`` shapes, same ``rates_at()`` / ``compute_cost_usd()``
semantics, same longest-prefix model match, same ``None``-for-unknown
discipline, :class:`Decimal` end-to-end. Vertex is Ogma's THIRD vendor
(after Anthropic and OpenAI); keeping all three rate modules
structurally identical lets the cross-vendor cost path
(:mod:`vargate_telemetry.pricing.vendor_cost`) treat them
interchangeably.

Pricing is **versioned by date range** (see ``RATE_HISTORY``):
historical records must compute against the rate active **when
``occurred_at`` happened**, not today's rate. When a published Gemini
rate next changes, add a new :class:`RateCardEntry` and freeze the OLD
rates in the prior entry — DO NOT mutate an existing entry.

Units
-----

All rates are USD **per million tokens** (Google's published unit for
the Gemini API / Vertex AI generative models). Token counts arrive as
``int``; the helper divides by 1_000_000 before multiplying.
:class:`Decimal` end-to-end to avoid float drift on aggregate sums.

The Vertex difference — context-length tiering
----------------------------------------------

Google prices **Gemini 2.5 Pro by prompt (context) length**: a request
whose input is ``<= 200K`` tokens bills at one rate, and a request whose
input is ``> 200K`` tokens bills at a HIGHER rate (both input and output
step up). Neither Anthropic nor OpenAI tiers this way, so this is the
one structural addition over ``openai_rates``:

  - Non-tiered models (2.0 Flash, 2.5 Flash, 2.0 Flash-Lite, …) carry a
    plain :class:`ModelRates`, exactly like the other two vendors.
  - 2.5 Pro carries a :class:`TieredModelRates` — a threshold plus the
    two :class:`ModelRates` slices. ``compute_cost_usd`` resolves it to
    a concrete :class:`ModelRates` using the request's input-token count
    as the context-length proxy (see :func:`_resolve_rates`). The
    threshold is on **input** tokens (the prompt length), which is what
    Google's tier boundary is defined on.

Everything downstream of :func:`_resolve_rates` is identical to the
other vendors — the tiering is confined to one explicit, documented
seam so the rest of the machinery (history walk, longest-prefix lookup,
Decimal math) stays byte-for-byte the established shape.

Cache pricing
-------------

Gemini's API exposes **cached-input** (context-cache) pricing at a
reduced per-token rate, similar to OpenAI's automatic prompt caching.
As with OpenAI there is **no separate cache-creation (cache-write)
charge** on the per-token rate card (Google bills context-cache storage
by time/volume, which is out of scope for a per-token estimate), so:

  - ``cache_creation_per_mtok`` is **always 0** for every Gemini model.
    The field exists only to keep the :class:`ModelRates` shape
    identical to the other two vendors; Vertex callers pass
    ``cache_creation_tokens=0`` and it contributes nothing to cost.
  - ``cache_read_per_mtok`` is the discounted cached-input rate. Models
    without a published cached-input price carry
    ``cache_read_per_mtok == input_per_mtok`` so a stray cached count
    bills at the normal rate rather than free.

Attribution note
-----------------

Google has **no per-user-email attribution** — Vertex usage attributes
to project and team (request labels) only. That is a concern of the
ingest / dims path, not this module: pricing is per-token-and-model and
identical regardless of who ran the request.

Source of truth
---------------

Rates per 1M tokens, USD. The Gemini lineup and its published prices
are summarized in the TM9 desk recon
(``docs/sprints/TM9-vertex-ingest-recon.md`` — Phase A). They are
MEDIUM confidence until re-confirmed against this org's live BigQuery
billing export, so each entry carries a Phase-A re-confirm ``TODO``.
Update this module when Google bumps a rate; add a new
:class:`RateCardEntry` — never mutate an existing one.

# TODO(TM9 Phase A): re-confirm the entire 2.x rate card (every
# ``ModelRates`` / ``TieredModelRates`` below) against this org's live
# Vertex billing-export SKU prices once a real GCP project is bound.
# Desk-recon figures (retrieved 2026-06-09) are subject to Google's
# pricing-page churn and the "Gemini Enterprise Agent Platform" /
# Vertex rebrand. The 2.5 Pro 200K context-length tier boundary and
# both of its rate slices in particular must be confirmed against a
# real billed line item.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Union


# ───────────────────────────────────────────────────────────────────────────
# Per-model rate shape  (identical to openai_rates.ModelRates)
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelRates:
    """USD per million tokens for one model under one rate card.

    All four fields exist so the shape matches Anthropic's and OpenAI's
    and the cross-vendor cost path is uniform. For Vertex/Gemini:
      - ``cache_creation_per_mtok`` is always 0 (no per-token
        cache-write charge);
      - ``cache_read_per_mtok`` is the discounted cached-input rate
        (or == ``input_per_mtok`` for models with no published cached
        rate).
    """

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal
    cache_creation_per_mtok: Decimal


@dataclass(frozen=True)
class TieredModelRates:
    """Context-length-tiered rates for one model (Gemini 2.5 Pro).

    Google bills 2.5 Pro at a higher rate once the **input (prompt)**
    exceeds ``input_token_threshold`` tokens. ``low`` is the rate for a
    request whose input is ``<= input_token_threshold``; ``high`` is the
    rate for ``> input_token_threshold``. :func:`_resolve_rates`
    collapses this to a concrete :class:`ModelRates` given a request's
    input-token count.

    Keeping this a distinct frozen dataclass (rather than overloading
    :class:`ModelRates`) means the non-tiered models — and the other two
    vendors — are untouched, and the tiering lives in exactly one place.
    """

    input_token_threshold: int
    low: ModelRates
    high: ModelRates


# A rate-map value is either a flat rate (most models) or a tiered rate
# (2.5 Pro). ``_lookup_model`` returns whichever is registered; the tier
# is only resolved later, by ``compute_cost_usd`` via ``_resolve_rates``,
# because resolution needs the request's input-token count.
RateValue = Union[ModelRates, TieredModelRates]


@dataclass(frozen=True)
class RateCardEntry:
    """One slice of pricing history.

    ``effective_from`` inclusive; ``effective_to`` exclusive (None =
    open-ended, the current rate). ``rates`` is keyed by the Gemini
    model string and matched exact-OR-longest-prefix in ``rates_at``
    (see ``_lookup_model``). Values are :class:`ModelRates` (flat) or
    :class:`TieredModelRates` (context-length-tiered, 2.5 Pro).
    """

    effective_from: datetime
    effective_to: Optional[datetime]
    rates: dict[str, RateValue]


# ───────────────────────────────────────────────────────────────────────────
# Rate history
# ───────────────────────────────────────────────────────────────────────────
#
# Vertex billing export reports usage against a Gemini SKU / model
# string (e.g. ``gemini-2.5-pro``, ``gemini-2.0-flash-001``). We key the
# rate dict by the **family** form (``gemini-2.5-pro``,
# ``gemini-2.0-flash``); the longest-prefix fallback in
# ``_lookup_model`` maps a version-stamped variant
# (``gemini-2.0-flash-001``) to the family rate, exactly like
# openai_rates. Unknown families fall through to ``None`` — never fake a
# rate.
#
# Longest-prefix (not first-match) is essential here: ``gemini-2.0-flash``
# must NOT swallow ``gemini-2.0-flash-lite`` — the more specific
# (longer) key wins. List order below is irrelevant; _lookup_model picks
# the longest matching key.

# The 2026-current rate card. All cache_creation = 0 (Gemini has no
# per-token cache-write charge). cache_read is the published
# cached-input rate (== input where the model has no published cached
# price).
#
# 2.5 Pro is context-length-tiered: <=200K input tokens bills at the
# ``low`` slice, >200K at the ``high`` slice.
_RATES_2026: dict[str, RateValue] = {
    # ── Gemini 2.5 family ──
    # 2.5 Pro — context-length-tiered (<=200K vs >200K input tokens).
    # TODO(TM9 Phase A): confirm the 200K boundary and BOTH slices
    # (low 1.25/10.00, high 2.50/15.00 per 1M tok) against a real
    # billed line item; cached-input rate is the desk-recon estimate.
    "gemini-2.5-pro": TieredModelRates(
        input_token_threshold=200_000,
        low=ModelRates(
            input_per_mtok=Decimal("1.25"),
            output_per_mtok=Decimal("10.00"),
            # Cached input ≈ 25% of input (desk recon); reduced rate.
            cache_read_per_mtok=Decimal("0.3125"),
            cache_creation_per_mtok=Decimal("0"),
        ),
        high=ModelRates(
            input_per_mtok=Decimal("2.50"),
            output_per_mtok=Decimal("15.00"),
            cache_read_per_mtok=Decimal("0.625"),
            cache_creation_per_mtok=Decimal("0"),
        ),
    ),
    # 2.5 Flash — flat (no context-length tier).
    # NOTE: longer prefix than "gemini-2.5" but the rate map has no bare
    # "gemini-2.5" key, so no collision; listed for clarity only.
    "gemini-2.5-flash": ModelRates(
        input_per_mtok=Decimal("0.30"),
        output_per_mtok=Decimal("2.50"),
        # Cached input ≈ 25% of input (desk recon).
        cache_read_per_mtok=Decimal("0.075"),
        cache_creation_per_mtok=Decimal("0"),
    ),
    # ── Gemini 2.0 family ──
    # 2.0 Flash-Lite — flat. MUST out-prefix "gemini-2.0-flash" (longer
    # key), which _lookup_model guarantees via longest-prefix match.
    "gemini-2.0-flash-lite": ModelRates(
        input_per_mtok=Decimal("0.075"),
        output_per_mtok=Decimal("0.30"),
        # No published cached-input rate → == input (stray cached count
        # bills normally, not free).
        cache_read_per_mtok=Decimal("0.075"),
        cache_creation_per_mtok=Decimal("0"),
    ),
    # 2.0 Flash — flat.
    "gemini-2.0-flash": ModelRates(
        input_per_mtok=Decimal("0.15"),
        output_per_mtok=Decimal("0.60"),
        # Cached input ≈ 25% of input (desk recon).
        cache_read_per_mtok=Decimal("0.0375"),
        cache_creation_per_mtok=Decimal("0"),
    ),
}


RATE_HISTORY: list[RateCardEntry] = [
    # Single open-ended entry for now. Google's published rates for the
    # models we cover are treated as stable through the TM9 window; the
    # first real rate change adds a SECOND entry with these rates frozen
    # as the prior (closed) window. effective_from is set well before
    # any record we'd ingest so a backfill never falls below history
    # (rates_at is defensive anyway).
    #
    # TODO(TM9 Phase A): set effective_from to the earliest billing
    # export date a real customer project actually carries, once known;
    # 2024-01-01 is a safe-low placeholder.
    RateCardEntry(
        effective_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
        effective_to=None,
        rates=_RATES_2026,
    ),
]


CURRENT_RATES: dict[str, RateValue] = RATE_HISTORY[-1].rates


# ───────────────────────────────────────────────────────────────────────────
# Helpers  (identical semantics to openai_rates, plus tier resolution)
# ───────────────────────────────────────────────────────────────────────────


def rates_at(occurred_at: datetime) -> dict[str, RateValue]:
    """Return the rate map active at ``occurred_at``.

    Walks ``RATE_HISTORY`` newest-first and returns the first entry
    whose ``[effective_from, effective_to)`` window covers the
    timestamp. Defensive: if ``occurred_at`` precedes the oldest entry,
    returns the oldest entry's rates anyway — better than crashing, and
    historical accuracy beyond our coverage window isn't a billing
    concern.
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
    model: str, rates: dict[str, RateValue]
) -> Optional[RateValue]:
    """Look up a model's rates with a longest-prefix fallback.

    Exact match wins. If exact fails, try the longest key in the rate
    map that the model name starts with — covers Vertex's
    version-stamped variants: ``gemini-2.0-flash-001`` →
    ``gemini-2.0-flash``. Longest-prefix (not first-match) is essential
    here: ``gemini-2.0-flash-lite`` must NOT be swallowed by the shorter
    ``gemini-2.0-flash`` key.

    Returns the registered value — :class:`ModelRates` (flat) or
    :class:`TieredModelRates` (2.5 Pro). Returns ``None`` for unknown
    families. **Never fakes a rate.**
    """
    exact = rates.get(model)
    if exact is not None:
        return exact

    best: Optional[RateValue] = None
    best_len = 0
    for key, val in rates.items():
        if model.startswith(key) and len(key) > best_len:
            best = val
            best_len = len(key)
    return best


def _resolve_rates(rate_value: RateValue, input_tokens: int) -> ModelRates:
    """Collapse a (possibly tiered) rate value to a concrete
    :class:`ModelRates` given a request's input-token count.

    Flat :class:`ModelRates` is returned unchanged. A
    :class:`TieredModelRates` (2.5 Pro) is resolved by comparing
    ``input_tokens`` to its ``input_token_threshold``: ``<= threshold``
    selects the ``low`` slice, ``> threshold`` selects ``high``. The
    boundary is on **input** tokens because that is the prompt/context
    length Google's tier is defined on.
    """
    if isinstance(rate_value, TieredModelRates):
        if input_tokens > rate_value.input_token_threshold:
            return rate_value.high
        return rate_value.low
    return rate_value


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
    ``openai_rates.compute_cost_usd`` / ``anthropic_rates.compute_cost_usd``
    so the cross-vendor cost path can call any of the three. Vertex
    callers map the usage row as:
      - ``input_tokens``          = uncached input tokens   (full rate)
      - ``cache_read_tokens``     = cached input tokens      (cached rate)
      - ``cache_creation_tokens`` = 0  (Gemini has no per-token
        cache-write charge)
      - ``output_tokens``         = output tokens

    For the context-length-tiered model (2.5 Pro) the tier is selected
    from ``input_tokens`` — pass the request's actual (uncached) input
    count, not a cached subset, so the prompt-length tier boundary
    resolves correctly.

    Returns ``None`` when:
      - ``model`` is ``None``;
      - the model name is unknown to the rate card (surface the gap,
        don't fake a number).

    Returns a :class:`Decimal` rounded to six decimal places — same as
    the other two vendors. Aggregate the per-row Decimals and round the
    TOTAL to two decimals; rounding each row independently would
    compound error.

    Note: because every Gemini ``cache_creation_per_mtok`` is 0, the
    ``cache_creation_tokens`` term contributes nothing even if a caller
    passes a non-zero count — the no-per-token-cache-write rule holds by
    construction, not just by caller convention.
    """
    if model is None:
        return None

    rate_value = _lookup_model(model, rates_at(occurred_at))
    if rate_value is None:
        return None

    rates = _resolve_rates(rate_value, input_tokens)

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
