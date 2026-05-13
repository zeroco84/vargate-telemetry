# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the T5.5.6 Anthropic pricing rate card + cost helper.

Covers:
  - Current-rate compute against the published Anthropic numbers.
  - Versioned lookup: rates_at returns the right entry across rate
    bumps.
  - Prefix-match fallback for date-stamped model variants.
  - Unknown model → None (never fake a rate).
  - Null model → None (legacy aggregate rows).
  - Cache-read and cache-creation use the discounted rate, not the
    fresh-input rate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from vargate_telemetry.pricing.anthropic_rates import (
    CURRENT_RATES,
    RATE_HISTORY,
    compute_cost_usd,
    rates_at,
)


# ───────────────────────────────────────────────────────────────────────────
# Current rates
# ───────────────────────────────────────────────────────────────────────────


def test_sonnet_45_current_rates_match_published() -> None:
    """Sonnet 4.5 is $3 input / $15 output per MTok."""
    rates = CURRENT_RATES["claude-sonnet-4-5-20250929"]
    assert rates.input_per_mtok == Decimal("3.00")
    assert rates.output_per_mtok == Decimal("15.00")
    assert rates.cache_read_per_mtok == Decimal("0.30")
    assert rates.cache_creation_per_mtok == Decimal("3.75")


def test_opus_47_current_rates_match_published() -> None:
    """Opus 4.7 is $15 input / $75 output per MTok."""
    rates = CURRENT_RATES["claude-opus-4-7"]
    assert rates.input_per_mtok == Decimal("15.00")
    assert rates.output_per_mtok == Decimal("75.00")


def test_haiku_45_current_rates_match_published() -> None:
    rates = CURRENT_RATES["claude-haiku-4-5"]
    assert rates.input_per_mtok == Decimal("1.00")
    assert rates.output_per_mtok == Decimal("5.00")


# ───────────────────────────────────────────────────────────────────────────
# compute_cost_usd happy path
# ───────────────────────────────────────────────────────────────────────────


def test_compute_cost_sonnet_45_combines_all_four_buckets() -> None:
    """Sonnet 4.5 with token counts across all four buckets:
       1M input × $3   = $3.00
       1M output × $15 = $15.00
       1M cache read × $0.30 = $0.30
       1M cache write × $3.75 = $3.75
       Total = $22.05
    """
    cost = compute_cost_usd(
        "claude-sonnet-4-5-20250929",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
        occurred_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    assert cost == Decimal("22.050000")


def test_compute_cost_cache_read_uses_discounted_rate() -> None:
    """A breakdown with only cache_read costs the cache-read rate,
    NOT the fresh-input rate — proves the bucket separation."""
    cost = compute_cost_usd(
        "claude-sonnet-4-5-20250929",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=10_000_000,  # 10M tokens
        cache_creation_tokens=0,
        occurred_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    # 10M × $0.30 / MTok = $3.00. NOT $30 (which would be the
    # input rate × 10).
    assert cost == Decimal("3.000000")


def test_compute_cost_returns_decimal_not_float() -> None:
    """Aggregate sums shouldn't drift due to float — return Decimal."""
    cost = compute_cost_usd(
        "claude-sonnet-4-5-20250929",
        input_tokens=180_593,
        output_tokens=26_235,
        cache_read_tokens=689_700,
        cache_creation_tokens=0,
        occurred_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    assert isinstance(cost, Decimal)
    # 180_593 * 3 / 1M = 0.541779
    # 26_235 * 15 / 1M = 0.393525
    # 689_700 * 0.30 / 1M = 0.206910
    # Total = 1.142214
    assert cost == Decimal("1.142214")


# ───────────────────────────────────────────────────────────────────────────
# Versioned lookup
# ───────────────────────────────────────────────────────────────────────────


def test_rates_at_returns_current_for_today() -> None:
    """A recent timestamp matches the current entry."""
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    rates = rates_at(now)
    assert "claude-opus-4-7" in rates, (
        "Opus 4.7 must be in the current rate card"
    )


def test_rates_at_returns_old_rates_before_cutoff() -> None:
    """A pre-Opus-4.7 timestamp falls through to the prior entry,
    which doesn't have Opus 4.7 keyed."""
    pre_opus_47 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rates = rates_at(pre_opus_47)
    assert "claude-opus-4-7" not in rates, (
        "Opus 4.7 didn't exist before 2026-03-01; the prior rate "
        "card must not advertise it"
    )
    # Sonnet 4.5 did exist; it's in both cards.
    assert "claude-sonnet-4-5-20250929" in rates


def test_rates_at_naive_datetime_treated_as_utc() -> None:
    """A tz-naive datetime gets treated as UTC, not local."""
    naive = datetime(2026, 5, 11)
    aware = datetime(2026, 5, 11, tzinfo=timezone.utc)
    assert rates_at(naive) is rates_at(aware)


def test_rates_at_older_than_history_falls_through_to_oldest() -> None:
    """Defensive: a pre-2025 timestamp shouldn't crash — return the
    oldest available entry."""
    ancient = datetime(2020, 1, 1, tzinfo=timezone.utc)
    rates = rates_at(ancient)
    assert rates is RATE_HISTORY[0].rates


# ───────────────────────────────────────────────────────────────────────────
# Prefix-match fallback
# ───────────────────────────────────────────────────────────────────────────


def test_compute_cost_unknown_date_stamped_variant_falls_to_family() -> None:
    """A new date-stamped Opus 4.7 variant (e.g., 20260601) falls
    through to claude-opus-4-7's rates via prefix match."""
    cost = compute_cost_usd(
        "claude-opus-4-7-20260601",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    # 1M × $15 / MTok = $15.00 (Opus rate, not Sonnet's $3)
    assert cost == Decimal("15.000000")


# ───────────────────────────────────────────────────────────────────────────
# None paths (never fake a rate)
# ───────────────────────────────────────────────────────────────────────────


def test_compute_cost_null_model_returns_none() -> None:
    """Legacy aggregate rows have model=null; we can't compute cost."""
    cost = compute_cost_usd(
        None,
        input_tokens=180_593,
        output_tokens=26_235,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    assert cost is None


def test_compute_cost_unknown_model_returns_none() -> None:
    """An unknown model family (e.g., a hypothetical future Gemini-like
    name) returns None, NOT a faked figure."""
    cost = compute_cost_usd(
        "some-other-vendor-model-v1",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    assert cost is None


def test_compute_cost_zero_tokens_is_zero_not_none() -> None:
    """A known model with zero tokens still computes — returns 0,
    not None. The UI distinguishes 'computed and got zero' from 'we
    don't know'."""
    cost = compute_cost_usd(
        "claude-sonnet-4-5-20250929",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    assert cost == Decimal("0.000000")
