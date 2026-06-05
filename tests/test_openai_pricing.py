# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the TM8 OpenAI pricing rate card + cost helper.

Mirrors ``tests/test_pricing.py`` (Anthropic). Covers:
  - gpt-4o anchor rates ($2.50 in / $1.25 cached / $10 out) — the
    figures confirmed against live /costs in recon.
  - Longest-prefix match on date-stamped model names
    (``gpt-4o-2024-08-06`` → ``gpt-4o``) AND the gpt-4o-mini /
    gpt-4o disambiguation that a naive prefix match would get wrong.
  - Cached-input half-price math (OpenAI auto cache, ~50% off).
  - cache_creation contributes 0 to cost (OpenAI has no cache-write
    charge) — by construction, even for a non-zero token count.
  - Unknown model → None; null model → None (never fake a rate).
  - occurred_at lookup walks RATE_HISTORY across a rate change.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from vargate_telemetry.pricing import openai_rates as oair
from vargate_telemetry.pricing.openai_rates import (
    CURRENT_RATES,
    RATE_HISTORY,
    ModelRates,
    RateCardEntry,
    compute_cost_usd,
    rates_at,
)

# A timestamp inside the current (open-ended) OpenAI rate window.
_NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


# ───────────────────────────────────────────────────────────────────────────
# Anchor rates — confirmed against live /costs
# ───────────────────────────────────────────────────────────────────────────


def test_gpt4o_anchor_rates_match_confirmed() -> None:
    """gpt-4o is $2.50 input / $1.25 cached / $10.00 output per MTok,
    and cache_creation is 0 (OpenAI has no cache-write charge)."""
    rates = CURRENT_RATES["gpt-4o"]
    assert rates.input_per_mtok == Decimal("2.50")
    assert rates.output_per_mtok == Decimal("10.00")
    assert rates.cache_read_per_mtok == Decimal("1.25")
    assert rates.cache_creation_per_mtok == Decimal("0")


def test_gpt4o_mini_anchor_rates_match_confirmed() -> None:
    """gpt-4o-mini is $0.15 input / $0.075 cached / $0.60 output."""
    rates = CURRENT_RATES["gpt-4o-mini"]
    assert rates.input_per_mtok == Decimal("0.15")
    assert rates.output_per_mtok == Decimal("0.60")
    assert rates.cache_read_per_mtok == Decimal("0.075")
    assert rates.cache_creation_per_mtok == Decimal("0")


def test_every_openai_model_has_zero_cache_creation() -> None:
    """OpenAI has NO cache-write charge — every model's
    cache_creation_per_mtok must be exactly 0, so the canonical
    record's cache_creation_tokens can never add cost."""
    for name, rates in CURRENT_RATES.items():
        assert rates.cache_creation_per_mtok == Decimal("0"), (
            f"{name} must have cache_creation_per_mtok == 0 (OpenAI has "
            f"no cache-write charge)"
        )


def test_embedding_models_have_zero_output_rate() -> None:
    """Embedding models are input-only — output_per_mtok must be 0."""
    for name in ("text-embedding-3-small", "text-embedding-3-large"):
        assert CURRENT_RATES[name].output_per_mtok == Decimal("0"), (
            f"{name} is input-only; output rate must be 0"
        )


# ───────────────────────────────────────────────────────────────────────────
# gpt-4o anchor compute — the $2.50 / $10.00 figures
# ───────────────────────────────────────────────────────────────────────────


def test_compute_cost_gpt4o_input_anchor() -> None:
    """1M fresh input on gpt-4o = $2.50 (the confirmed anchor)."""
    cost = compute_cost_usd(
        "gpt-4o",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    assert cost == Decimal("2.500000")


def test_compute_cost_gpt4o_output_anchor() -> None:
    """1M output on gpt-4o = $10.00 (the confirmed anchor)."""
    cost = compute_cost_usd(
        "gpt-4o",
        input_tokens=0,
        output_tokens=1_000_000,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    assert cost == Decimal("10.000000")


def test_compute_cost_gpt4o_combined() -> None:
    """A mixed gpt-4o row exercises all three priced buckets:
       1M input  × $2.50  = $2.50
       1M output × $10.00 = $10.00
       1M cached × $1.25  = $1.25
       Total = $13.75 (cache_creation contributes 0).
    """
    cost = compute_cost_usd(
        "gpt-4o",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,  # OpenAI rate is 0 → no effect
        occurred_at=_NOW,
    )
    assert cost == Decimal("13.750000")


def test_compute_cost_returns_decimal_not_float() -> None:
    """Realistic token counts → a Decimal, never a float."""
    cost = compute_cost_usd(
        "gpt-4o",
        input_tokens=89,
        output_tokens=123,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    assert isinstance(cost, Decimal)
    # 89 * 2.50 / 1M  = 0.0002225
    # 123 * 10.0 / 1M = 0.0012300
    # Total           = 0.0014525
    # quantize() is ROUND_HALF_EVEN (banker's rounding, the Decimal
    # default): the trailing 5 rounds the 2 to the nearest EVEN digit,
    # which is 2 — so 0.001452, not 0.001453.
    assert cost == Decimal("0.001452")  # rounded to 6dp, half-even


# ───────────────────────────────────────────────────────────────────────────
# Longest-prefix match on date-stamped names
# ───────────────────────────────────────────────────────────────────────────


def test_date_stamped_gpt4o_maps_to_family() -> None:
    """gpt-4o-2024-08-06 (the real shape from the usage API) falls
    through to gpt-4o's rate via longest-prefix match."""
    cost = compute_cost_usd(
        "gpt-4o-2024-08-06",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    assert cost == Decimal("2.500000")  # gpt-4o input rate


def test_date_stamped_o4_mini_maps_to_family() -> None:
    """o4-mini-2025-04-16 → o4-mini's $0.55 input rate."""
    cost = compute_cost_usd(
        "o4-mini-2025-04-16",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    assert cost == Decimal("0.550000")


def test_gpt4o_mini_not_swallowed_by_gpt4o_prefix() -> None:
    """CRITICAL longest-prefix case: 'gpt-4o-mini' starts with the
    shorter 'gpt-4o' key, but must resolve to the gpt-4o-mini rate
    ($0.15), NOT gpt-4o's ($2.50). A first-match prefix scan would
    get this wrong."""
    cost = compute_cost_usd(
        "gpt-4o-mini",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    assert cost == Decimal("0.150000")


def test_date_stamped_gpt4o_mini_maps_to_mini_not_base() -> None:
    """gpt-4o-mini-2024-07-18 → gpt-4o-mini ($0.15), not gpt-4o."""
    cost = compute_cost_usd(
        "gpt-4o-mini-2024-07-18",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    assert cost == Decimal("0.150000")


def test_gpt41_mini_not_swallowed_by_gpt41_prefix() -> None:
    """gpt-4.1-mini ($0.40) must not resolve to gpt-4.1 ($2.00)."""
    cost = compute_cost_usd(
        "gpt-4.1-mini-2025-04-14",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    assert cost == Decimal("0.400000")


# ───────────────────────────────────────────────────────────────────────────
# Cached-input half-price math (OpenAI auto cache)
# ───────────────────────────────────────────────────────────────────────────


def test_cached_input_is_half_of_fresh_input_gpt4o() -> None:
    """gpt-4o cached input ($1.25) is exactly half of fresh input
    ($2.50) — the ~50% automatic-cache discount. 10M cached tokens
    = $12.50, and that's half of what 10M fresh input would cost."""
    cached = compute_cost_usd(
        "gpt-4o",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=10_000_000,
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    fresh = compute_cost_usd(
        "gpt-4o",
        input_tokens=10_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    assert cached == Decimal("12.500000")
    assert fresh == Decimal("25.000000")
    assert cached * 2 == fresh


def test_cached_input_half_price_gpt4o_mini() -> None:
    """gpt-4o-mini cached ($0.075) is half of fresh ($0.15)."""
    cost = compute_cost_usd(
        "gpt-4o-mini",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    assert cost == Decimal("0.075000")


def test_double_count_trap_uncached_plus_cached() -> None:
    """The §2.1 recon trap, modeled correctly: a usage row with
    input_uncached=600k and input_cached=400k bills the two splits
    at their respective rates — NOT the 1M total at the full rate.
       600k × $2.50 / 1M = $1.50
       400k × $1.25 / 1M = $0.50
       Total = $2.00  (vs $2.50 if the 1M total were billed full-rate)
    """
    cost = compute_cost_usd(
        "gpt-4o",
        input_tokens=600_000,  # = input_uncached_tokens
        output_tokens=0,
        cache_read_tokens=400_000,  # = input_cached_tokens
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    assert cost == Decimal("2.000000")


# ───────────────────────────────────────────────────────────────────────────
# cache_creation contributes 0
# ───────────────────────────────────────────────────────────────────────────


def test_cache_creation_contributes_zero() -> None:
    """A row with ONLY cache_creation_tokens costs $0 — OpenAI has no
    cache-write charge, so the rate is 0 and the term vanishes."""
    cost = compute_cost_usd(
        "gpt-4o",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=50_000_000,  # huge — still $0
        occurred_at=_NOW,
    )
    assert cost == Decimal("0.000000")


def test_cache_creation_does_not_change_total() -> None:
    """Adding any cache_creation count leaves an otherwise-identical
    row's cost unchanged."""
    base_kwargs = dict(
        input_tokens=123_456,
        output_tokens=7_890,
        cache_read_tokens=10_000,
        occurred_at=_NOW,
    )
    without = compute_cost_usd(
        "gpt-4o", cache_creation_tokens=0, **base_kwargs
    )
    with_creation = compute_cost_usd(
        "gpt-4o", cache_creation_tokens=999_999, **base_kwargs
    )
    assert without == with_creation


# ───────────────────────────────────────────────────────────────────────────
# None paths (never fake a rate)
# ───────────────────────────────────────────────────────────────────────────


def test_unknown_model_returns_none() -> None:
    """An unknown model family returns None, not a faked figure."""
    cost = compute_cost_usd(
        "gpt-9-superduper",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    assert cost is None


def test_null_model_returns_none() -> None:
    """A null model (plain aggregate usage row) → None."""
    cost = compute_cost_usd(
        None,
        input_tokens=1_000_000,
        output_tokens=500_000,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    assert cost is None


def test_anthropic_model_unknown_to_openai_card() -> None:
    """A Claude model name is unknown to the OpenAI card → None.
    The two vendors' cards are independent; no accidental cross-match."""
    cost = compute_cost_usd(
        "claude-sonnet-4-5-20250929",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    assert cost is None


def test_known_model_zero_tokens_is_zero_not_none() -> None:
    """A known model with all-zero tokens computes to 0, NOT None —
    'computed and got zero' is distinct from 'we don't know'."""
    cost = compute_cost_usd(
        "gpt-4o",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=_NOW,
    )
    assert cost == Decimal("0.000000")


# ───────────────────────────────────────────────────────────────────────────
# occurred_at lookup walks RATE_HISTORY
# ───────────────────────────────────────────────────────────────────────────


def test_rates_at_returns_current_for_now() -> None:
    """A current timestamp resolves to the open-ended entry, which
    has gpt-4o keyed."""
    rates = rates_at(_NOW)
    assert "gpt-4o" in rates
    assert rates is RATE_HISTORY[-1].rates


def test_rates_at_naive_datetime_treated_as_utc() -> None:
    """A tz-naive datetime is treated as UTC, not local."""
    naive = datetime(2026, 6, 5)
    aware = datetime(2026, 6, 5, tzinfo=timezone.utc)
    assert rates_at(naive) is rates_at(aware)


def test_rates_at_older_than_history_falls_through_to_oldest() -> None:
    """Defensive: a pre-history timestamp returns the oldest entry
    rather than crashing."""
    ancient = datetime(2019, 1, 1, tzinfo=timezone.utc)
    assert rates_at(ancient) is RATE_HISTORY[0].rates


def test_occurred_at_walks_history_across_a_rate_change(monkeypatch) -> None:
    """Genuinely exercise the newest-first RATE_HISTORY walk with a
    synthetic TWO-entry history: an old window where gpt-4o was a
    placeholder $1.00, and the current window at the real $2.50.
    rates_at must pick the entry whose [from, to) covers occurred_at,
    and compute_cost_usd must bill against the then-current rate.

    This models exactly what happens the first time OpenAI bumps a
    published rate and we freeze the old one in a closed window.
    """
    cutover = datetime(2026, 1, 1, tzinfo=timezone.utc)

    old_rates = {
        "gpt-4o": ModelRates(
            input_per_mtok=Decimal("1.00"),  # the OLD (frozen) rate
            output_per_mtok=Decimal("4.00"),
            cache_read_per_mtok=Decimal("0.50"),
            cache_creation_per_mtok=Decimal("0"),
        )
    }
    new_rates = {
        "gpt-4o": ModelRates(
            input_per_mtok=Decimal("2.50"),  # the current rate
            output_per_mtok=Decimal("10.00"),
            cache_read_per_mtok=Decimal("1.25"),
            cache_creation_per_mtok=Decimal("0"),
        )
    }
    synthetic_history = [
        RateCardEntry(
            effective_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
            effective_to=cutover,
            rates=old_rates,
        ),
        RateCardEntry(
            effective_from=cutover,
            effective_to=None,
            rates=new_rates,
        ),
    ]
    monkeypatch.setattr(oair, "RATE_HISTORY", synthetic_history)

    # Before the cutover → old $1.00 rate.
    before = oair.compute_cost_usd(
        "gpt-4o",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    assert before == Decimal("1.000000")

    # After the cutover → current $2.50 rate.
    after = oair.compute_cost_usd(
        "gpt-4o",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    assert after == Decimal("2.500000")

    # And rates_at itself picks the right window object.
    assert oair.rates_at(
        datetime(2025, 6, 1, tzinfo=timezone.utc)
    ) is old_rates
    assert oair.rates_at(
        datetime(2026, 6, 1, tzinfo=timezone.utc)
    ) is new_rates
