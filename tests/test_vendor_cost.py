# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the cross-vendor cost primitive (TM8 Phase D).

Covers ``vargate_telemetry.pricing.vendor_cost``:

  - **Anthropic parity** — an ``admin`` usage record prices to the
    SAME number the existing ``anthropic_rates.compute_cost_usd`` gives
    for the same tokens (regression-safe drop-in), including the nested
    ``cache_creation`` ephemeral-sum fallback.
  - **OpenAI mapping + double-count guard** — an ``openai_admin_usage``
    record bills the uncached split at the input rate and the cached
    split at the cached rate; an ALL-CACHE row costs the CACHED rate,
    NOT the full input rate (the §2.1 trap).
  - **Unknown / non-usage source → None** — ``openai_admin_costs`` (read
    elsewhere, not estimated here), ``openai_audit_logs``, a non-usage
    Anthropic stream.
  - **Empty-bucket sentinels / null+unknown model → None** (never fakes
    a number).
  - **vendor_of mapping** — OpenAI streams → ``"OpenAI"``, everything
    else → ``"Anthropic"``.

Pure-Python (no DB) — the primitive takes a metadata dict + occurred_at
and dispatches on source_api, so it's directly unit-testable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from vargate_telemetry.pricing import anthropic_rates, openai_rates
from vargate_telemetry.pricing.vendor_cost import (
    VENDOR_ANTHROPIC,
    VENDOR_OPENAI,
    estimate_record_cost_usd,
    vendor_of,
)

# A timestamp inside both vendors' current (open-ended) rate windows.
_OCC = datetime(2026, 6, 5, tzinfo=timezone.utc)

# Sonnet: $3 input / $15 output per MTok.
_SONNET = "claude-sonnet-4-5-20250929"


# ───────────────────────────────────────────────────────────────────────────
# Helpers — build records exactly as the pull tasks write metadata.
# ───────────────────────────────────────────────────────────────────────────


def _anthropic_usage_md(
    *,
    model: str | None = _SONNET,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_creation: dict | None = None,
) -> dict:
    """Anthropic admin usage record metadata (one breakdown in results),
    matching ``pull_admin._normalize_usage``."""
    breakdown: dict = {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
    }
    if cache_creation is not None:
        breakdown["cache_creation"] = cache_creation
    else:
        breakdown["cache_creation_input_tokens"] = cache_creation_input_tokens
    return {
        "starting_at": _OCC.isoformat(),
        "ending_at": _OCC.isoformat(),
        "results": [breakdown],
    }


def _openai_usage_md(
    *,
    model: str | None = "gpt-4o",
    input_uncached_tokens: int = 0,
    input_cached_tokens: int = 0,
    output_tokens: int = 0,
) -> dict:
    """OpenAI admin usage record metadata (one result), matching
    ``pull_openai_usage._normalize_result``. ``input_tokens`` is the
    TOTAL (uncached + cached) — the field the primitive must NOT bill."""
    return {
        "start_time": _OCC.isoformat(),
        "end_time": _OCC.isoformat(),
        "modality": "completions",
        "result": {
            "model": model,
            "input_tokens": input_uncached_tokens + input_cached_tokens,
            "input_uncached_tokens": input_uncached_tokens,
            "input_cached_tokens": input_cached_tokens,
            "output_tokens": output_tokens,
        },
        "model": model,
        "subject_user_id": "user-x",
    }


# ───────────────────────────────────────────────────────────────────────────
# vendor_of
# ───────────────────────────────────────────────────────────────────────────


def test_vendor_of_openai_streams() -> None:
    """Every ``openai_*`` source_api → "OpenAI"."""
    assert vendor_of("openai_admin_usage") == VENDOR_OPENAI
    assert vendor_of("openai_admin_costs") == VENDOR_OPENAI
    assert vendor_of("openai_audit_logs") == VENDOR_OPENAI
    # Prefix match → a future openai_* stream classifies correctly.
    assert vendor_of("openai_something_new") == VENDOR_OPENAI


def test_vendor_of_anthropic_and_default() -> None:
    """Anthropic streams + any non-openai source → "Anthropic"."""
    assert vendor_of("admin") == VENDOR_ANTHROPIC
    assert vendor_of("mcp") == VENDOR_ANTHROPIC
    assert vendor_of("code_analytics") == VENDOR_ANTHROPIC
    assert vendor_of("activity_feed") == VENDOR_ANTHROPIC
    assert vendor_of("compliance_content") == VENDOR_ANTHROPIC


# ───────────────────────────────────────────────────────────────────────────
# Anthropic parity — regression-safe drop-in
# ───────────────────────────────────────────────────────────────────────────


def test_anthropic_parity_known_record() -> None:
    """An admin record prices to the exact same Decimal the existing
    ``anthropic_rates.compute_cost_usd`` returns for the same tokens.

    1M input + 200k output at Sonnet = $6.00 — and crucially the
    primitive must equal the direct call bit-for-bit (it IS the per-row
    path ``api/usage.py`` uses, just dispatched)."""
    md = _anthropic_usage_md(input_tokens=1_000_000, output_tokens=200_000)

    got = estimate_record_cost_usd("admin", md, _OCC)
    ref = anthropic_rates.compute_cost_usd(
        _SONNET,
        input_tokens=1_000_000,
        output_tokens=200_000,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        occurred_at=_OCC,
    )
    assert got == ref
    assert got == Decimal("6.000000")


def test_anthropic_parity_nested_cache_creation() -> None:
    """When the flat ``cache_creation_input_tokens`` is absent, the
    primitive sums the nested ``cache_creation`` ephemeral_5m + 1h
    fields — the same COALESCE/NULLIF fallback ``api/usage.py`` does in
    SQL. 1.5M cache-creation tokens at Sonnet's $3.75/MTok write rate."""
    md = _anthropic_usage_md(
        input_tokens=0,
        output_tokens=0,
        cache_creation={
            "ephemeral_5m_input_tokens": 1_000_000,
            "ephemeral_1h_input_tokens": 500_000,
        },
    )

    got = estimate_record_cost_usd("admin", md, _OCC)
    ref = anthropic_rates.compute_cost_usd(
        _SONNET,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=1_500_000,
        occurred_at=_OCC,
    )
    assert got == ref
    assert got == Decimal("5.625000")  # 1.5 * 3.75


def test_anthropic_flat_cache_creation_wins_over_nested() -> None:
    """A non-zero flat ``cache_creation_input_tokens`` takes priority
    over the nested sum (NULLIF only falls through on a 0 flat value)."""
    md = _anthropic_usage_md(
        cache_creation_input_tokens=400_000,
    )
    # Add a nested block too; the flat non-zero value must win.
    md["results"][0]["cache_creation"] = {
        "ephemeral_5m_input_tokens": 999_999,
        "ephemeral_1h_input_tokens": 999_999,
    }
    got = estimate_record_cost_usd("admin", md, _OCC)
    ref = anthropic_rates.compute_cost_usd(
        _SONNET,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=400_000,
        occurred_at=_OCC,
    )
    assert got == ref


# ───────────────────────────────────────────────────────────────────────────
# OpenAI mapping + double-count guard
# ───────────────────────────────────────────────────────────────────────────


def test_openai_uncached_cached_mapping() -> None:
    """uncached → input rate, cached → cached rate, output → output rate.

    gpt-4o: $2.50 input / $1.25 cached / $10.00 output. 600k uncached +
    400k cached + 100k output = 1.50 + 0.50 + 1.00 = $3.00."""
    md = _openai_usage_md(
        input_uncached_tokens=600_000,
        input_cached_tokens=400_000,
        output_tokens=100_000,
    )
    got = estimate_record_cost_usd("openai_admin_usage", md, _OCC)
    assert got == Decimal("3.000000")

    # Equals the direct openai_rates call with the double-count-safe map.
    ref = openai_rates.compute_cost_usd(
        "gpt-4o",
        input_tokens=600_000,
        output_tokens=100_000,
        cache_read_tokens=400_000,
        cache_creation_tokens=0,
        occurred_at=_OCC,
    )
    assert got == ref


def test_openai_double_count_guard_all_cache() -> None:
    """An ALL-CACHE row (input_tokens == input_cached_tokens, uncached=0)
    costs the CACHED rate, NOT the full input rate.

    1M total input, all cached, gpt-4o: cached $1.25/MTok = $1.25. If the
    primitive wrongly billed the raw ``input_tokens`` total it would be
    $2.50 — this is the §2.1 trap."""
    md = _openai_usage_md(
        input_uncached_tokens=0,
        input_cached_tokens=1_000_000,
        output_tokens=0,
    )
    got = estimate_record_cost_usd("openai_admin_usage", md, _OCC)
    assert got == Decimal("1.250000")  # cached rate
    assert got != Decimal("2.500000")  # NOT the full input rate


def test_openai_date_stamped_model_prefix_match() -> None:
    """A date-stamped OpenAI model name (``gpt-4o-2024-08-06``) resolves
    via longest-prefix to the gpt-4o family rate."""
    md = _openai_usage_md(
        model="gpt-4o-2024-08-06",
        input_uncached_tokens=1_000_000,
    )
    got = estimate_record_cost_usd("openai_admin_usage", md, _OCC)
    assert got == Decimal("2.500000")  # gpt-4o input rate


# ───────────────────────────────────────────────────────────────────────────
# None cases — unknown source, sentinels, unpriceable model
# ───────────────────────────────────────────────────────────────────────────


def test_openai_costs_stream_returns_none() -> None:
    """``openai_admin_costs`` is the AUTHORITATIVE billed stream read
    directly by spend_data — the primitive must NOT estimate it (would
    double-count against the usage estimate)."""
    md = {"amount_value": "5.00", "line_item": "gpt-4o, input"}
    assert estimate_record_cost_usd("openai_admin_costs", md, _OCC) is None


def test_unknown_and_nonusage_sources_return_none() -> None:
    """Audit logs + non-usage Anthropic streams aren't token-estimable."""
    assert estimate_record_cost_usd("openai_audit_logs", {}, _OCC) is None
    assert estimate_record_cost_usd("mcp", {"summary": "x"}, _OCC) is None
    assert (
        estimate_record_cost_usd("totally_unknown", {"k": 1}, _OCC) is None
    )


def test_empty_bucket_sentinels_return_none() -> None:
    """The empty-bucket sentinel records (no priceable breakdown) → None."""
    # OpenAI sentinel: result is None.
    assert (
        estimate_record_cost_usd(
            "openai_admin_usage", {"result": None}, _OCC
        )
        is None
    )
    # Anthropic sentinel: empty results list.
    assert (
        estimate_record_cost_usd("admin", {"results": []}, _OCC) is None
    )


def test_null_and_unknown_model_return_none() -> None:
    """A null model (legacy aggregate) or an unknown family → None;
    never fake a number."""
    null_model = _anthropic_usage_md(model=None, input_tokens=5_000)
    assert estimate_record_cost_usd("admin", null_model, _OCC) is None

    unknown = _openai_usage_md(
        model="not-a-real-model-xyz", input_uncached_tokens=5_000
    )
    assert (
        estimate_record_cost_usd("openai_admin_usage", unknown, _OCC)
        is None
    )


def test_non_dict_metadata_returns_none() -> None:
    """Defensive: a non-dict metadata (shouldn't happen, but RLS rows
    could carry odd shapes) → None rather than raising."""
    assert estimate_record_cost_usd("admin", None, _OCC) is None  # type: ignore[arg-type]
    assert estimate_record_cost_usd("openai_admin_usage", [], _OCC) is None  # type: ignore[arg-type]


def test_naive_occurred_at_treated_as_utc() -> None:
    """A naive datetime is treated as UTC (the ingest path stores UTC) —
    no crash, and prices the same as the tz-aware equivalent."""
    naive = datetime(2026, 6, 5)  # no tzinfo
    md = _anthropic_usage_md(input_tokens=1_000_000)
    got = estimate_record_cost_usd("admin", md, naive)
    aware = estimate_record_cost_usd("admin", md, _OCC)
    assert got == aware
    assert got is not None
