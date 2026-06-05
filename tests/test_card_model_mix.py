# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the model-mix trend card (TM7).

Exercises ``vargate_telemetry.insights.cards.model_mix.build_card``
directly (no HTTP) against synthetic ``telemetry_records`` seeded
through a direct INSERT, mirroring ``test_usage_api.py`` /
``test_budgets_api.py``.

The card compares per-model spend share over the trailing 7 days
against the immediately-preceding 7 days:

  - a >=30 percentage-point share swing (e.g. a Sonnet -> Opus
    migration that silently multiplies per-turn cost) -> ``advisory``
    with at least one finding;
  - an identical mix in both windows -> ``idle`` with zero findings.

Window placement is by ``occurred_at``: ``model_share`` reads the
window ``[now - offset - days, now - offset)`` in UTC, so the CURRENT
7d is ``[now-7d, now)`` and the PRIOR 7d is ``[now-14d, now-7d)``. We
place a record in the prior window by seeding it with an earlier
``occurred_at`` (~10 days ago); the current window gets a recent one
(~1 day ago).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text

os.environ.setdefault(
    "JWT_SIGNING_KEY",
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b",
)


# ───────────────────────────────────────────────────────────────────────────
# Fixtures + helpers
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_records():
    """Empty telemetry_records before AND after each test."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE")
        )
    yield
    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE")
        )


def _unique_tenant(name: str) -> str:
    return f"tnt_us_{name}_" + uuid.uuid4().hex[:8]


# Both models live in the current (2026-05) rate card, so tokens seeded
# anywhere in the trailing two weeks price to a non-zero USD cost.
_SONNET = "claude-sonnet-4-5-20250929"
_OPUS = "claude-opus-4-7"


def _seed_usage_record(
    tenant_id: str,
    *,
    occurred_at: datetime,
    model: str,
    input_tokens: int = 1_000_000,
    output_tokens: int = 200_000,
    workspace_id: str | None = None,
    api_key_id: str | None = None,
) -> None:
    """Insert one ``record_type='usage'`` / ``source_api='admin'`` row.

    Shape mirrors ``_seed_usage_record`` in ``test_budgets_api.py``:
    a ``metadata.results`` array with a single per-model breakdown,
    chain seq = COALESCE(MAX+1), placeholder content/prev/self hashes
    via ``decode(hex)``. ``occurred_at`` controls which 7d window the
    row lands in.
    """
    from vargate_telemetry.db import engine

    results = [
        {
            "model": model,
            "workspace_id": workspace_id,
            "api_key_id": api_key_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
    ]
    md = {
        "starting_at": occurred_at.isoformat(),
        "ending_at": occurred_at.isoformat(),
        "results": results,
    }
    eid = f"usage:{uuid.uuid4()}"
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, 'usage', 'admin', :eid,
                    :occurred_at, decode(:zero32, 'hex'),
                    :metadata,
                    (SELECT COALESCE(MAX(chain_seq), 0) + 1
                       FROM telemetry_records
                      WHERE tenant_id = :t_lookup),
                    decode(:zero32, 'hex'),
                    decode(:one32, 'hex')
                )
                """
            ),
            {
                "t": tenant_id,
                "t_lookup": tenant_id,
                "eid": eid,
                "occurred_at": occurred_at,
                "metadata": json.dumps(md),
                "zero32": "00" * 32,
                "one32": "11" * 32,
            },
        )


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# OpenAI usage seed (shape mirrors pull_openai_usage / test_vendor_spend).
# gpt-4o: input $2.50/MTok; gpt-4o-mini: input $0.15/MTok.
_GPT4O = "gpt-4o"
_GPT4O_MINI = "gpt-4o-mini"


def _seed_openai_usage(
    tenant_id: str,
    *,
    occurred_at: datetime,
    model: str,
    input_uncached: int = 1_000_000,
    input_cached: int = 0,
    output: int = 0,
) -> None:
    """Insert one ``record_type='usage'`` / ``source_api='openai_admin_usage'``
    row shaped like ``pull_openai_usage`` writes it."""
    from vargate_telemetry.db import engine

    md = {
        "start_time": occurred_at.isoformat(),
        "end_time": occurred_at.isoformat(),
        "modality": "completions",
        "result": {
            "model": model,
            "input_tokens": input_uncached + input_cached,  # TOTAL
            "input_uncached_tokens": input_uncached,
            "input_cached_tokens": input_cached,
            "output_tokens": output,
        },
        "model": model,
    }
    eid = f"openai_admin_usage:{uuid.uuid4()}"
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, 'usage', 'openai_admin_usage', :eid,
                    :occurred_at, decode(:zero32, 'hex'),
                    :metadata,
                    (SELECT COALESCE(MAX(chain_seq), 0) + 1
                       FROM telemetry_records
                      WHERE tenant_id = :t_lookup),
                    decode(:zero32, 'hex'),
                    decode(:one32, 'hex')
                )
                """
            ),
            {
                "t": tenant_id,
                "t_lookup": tenant_id,
                "eid": eid,
                "occurred_at": occurred_at,
                "metadata": json.dumps(md),
                "zero32": "00" * 32,
                "one32": "11" * 32,
            },
        )


# ───────────────────────────────────────────────────────────────────────────
# (a) >=30pp share swing → advisory with a finding
# ───────────────────────────────────────────────────────────────────────────


def test_share_swing_sonnet_to_opus_is_advisory(clean_records: None) -> None:
    """Prior 7d dominated by Sonnet, current 7d dominated by Opus
    (a 100-point swing) → severity ``advisory``, at least one finding,
    and an item whose detail shows the share transition (``->``) and
    whose value is a percentage-point delta (``pp``)."""
    from vargate_telemetry.insights.cards.model_mix import build_card

    tenant = _unique_tenant("modelmix_swing")
    now = _now()

    # PRIOR window [now-14d, now-7d): Sonnet only.
    _seed_usage_record(
        tenant,
        occurred_at=now - timedelta(days=10),
        model=_SONNET,
    )
    # CURRENT window [now-7d, now): Opus only.
    _seed_usage_record(
        tenant,
        occurred_at=now - timedelta(days=1),
        model=_OPUS,
    )

    card = build_card(tenant, "7d")

    assert card.id == "model_mix"
    assert card.severity == "advisory"
    assert card.findings_count >= 1
    assert len(card.items) >= 1

    # At least one item carries the share transition + a pp-delta value.
    # (The Opus row swung 0% -> 100%, a +100pp move.)
    assert any(
        it.detail is not None and "->" in it.detail for it in card.items
    ), [it.detail for it in card.items]
    assert any(
        it.value is not None and "pp" in it.value for it in card.items
    ), [it.value for it in card.items]


# ───────────────────────────────────────────────────────────────────────────
# (b) identical mix in both windows → idle, zero findings
# ───────────────────────────────────────────────────────────────────────────


def test_identical_mix_both_windows_is_idle(clean_records: None) -> None:
    """The same model mix in the prior and current 7d windows is not a
    shift → severity ``idle``, ``findings_count`` 0, no items."""
    from vargate_telemetry.insights.cards.model_mix import build_card

    tenant = _unique_tenant("modelmix_stable")
    now = _now()

    # Identical single-model (Sonnet) spend in each window.
    _seed_usage_record(
        tenant,
        occurred_at=now - timedelta(days=10),  # prior window
        model=_SONNET,
    )
    _seed_usage_record(
        tenant,
        occurred_at=now - timedelta(days=1),  # current window
        model=_SONNET,
    )

    card = build_card(tenant, "7d")

    assert card.id == "model_mix"
    assert card.severity == "idle"
    assert card.findings_count == 0
    assert card.items == []


# ───────────────────────────────────────────────────────────────────────────
# (c) Vendor-mix shift (TM8 Phase D)
# ───────────────────────────────────────────────────────────────────────────


def _vendor_items(card) -> list:
    """The vendor-shift findings on a card (label ends with '(vendor)')."""
    return [it for it in card.items if it.label.endswith("(vendor)")]


def test_vendor_shift_fires_on_20pp_share_swing(clean_records: None) -> None:
    """Prior week all-Anthropic, current week all-OpenAI → a >=20pp vendor
    share swing → advisory with at least one VENDOR-labelled finding whose
    detail mentions 'total spend' and whose value is a pp-delta."""
    from vargate_telemetry.insights.cards.model_mix import build_card

    tenant = _unique_tenant("vendorshift_20pp")
    now = _now()

    # PRIOR [now-14d, now-7d): Anthropic only.
    _seed_usage_record(
        tenant, occurred_at=now - timedelta(days=10), model=_SONNET
    )
    # CURRENT [now-7d, now): OpenAI only.
    _seed_openai_usage(
        tenant, occurred_at=now - timedelta(days=1), model=_GPT4O
    )

    card = build_card(tenant, "7d")

    assert card.severity == "advisory"
    vendor_items = _vendor_items(card)
    assert len(vendor_items) >= 1, [it.label for it in card.items]
    vi = vendor_items[0]
    assert vi.detail is not None and "total spend" in vi.detail
    assert vi.value is not None and "pp" in vi.value


def test_vendor_shift_fires_on_3x_absolute_with_stable_share(
    clean_records: None,
) -> None:
    """A vendor whose ABSOLUTE spend triples week-over-week fires the
    vendor-shift even when its SHARE doesn't move.

    The whole tenant scales up exactly 3x between weeks, holding the
    Anthropic/OpenAI proportions identical — so every vendor's SHARE is
    unchanged (0pp, well below the 20pp rule) and the trigger is the
    >=3x ABSOLUTE rule. (A uniform 3x also trips the per-model 3x rule,
    which is fine — we're asserting the vendor-shift path fired, and a
    vendor-labelled finding is unambiguous.)"""
    from vargate_telemetry.insights.cards.model_mix import build_card

    tenant = _unique_tenant("vendorshift_3x")
    now = _now()
    prior = now - timedelta(days=10)
    cur = now - timedelta(days=1)

    # PRIOR: Anthropic $6 (2M Sonnet), OpenAI $2.50 (1M gpt-4o). A≈71%.
    _seed_usage_record(
        tenant, occurred_at=prior, model=_SONNET, input_tokens=2_000_000,
        output_tokens=0,
    )
    _seed_openai_usage(
        tenant, occurred_at=prior, model=_GPT4O, input_uncached=1_000_000
    )
    # CURRENT: exactly 3x each vendor — Anthropic $18 (6M Sonnet), OpenAI
    # $7.50 (3M gpt-4o). Shares unchanged (still ≈71/29); each vendor 3x.
    _seed_usage_record(
        tenant, occurred_at=cur, model=_SONNET, input_tokens=6_000_000,
        output_tokens=0,
    )
    _seed_openai_usage(
        tenant, occurred_at=cur, model=_GPT4O, input_uncached=3_000_000
    )

    card = build_card(tenant, "7d")

    assert card.severity == "advisory"
    vendor_items = _vendor_items(card)
    assert len(vendor_items) >= 1, [it.label for it in card.items]
    # The share didn't move materially → the pp-delta on the vendor line
    # is below the 20pp bar; the finding fired on the 3x absolute rule.
    for vi in vendor_items:
        # value like "+0pp" / "-0pp" / "+1pp" — well under 20.
        pp = int(vi.value.replace("pp", "").replace("+", ""))
        assert abs(pp) < 20, vi.value


def test_vendor_shift_not_fired_below_thresholds(clean_records: None) -> None:
    """A small vendor-mix drift (< 20pp share, < 3x absolute on each
    vendor) and a stable per-model mix → NO vendor-shift finding (idle)."""
    from vargate_telemetry.insights.cards.model_mix import build_card

    tenant = _unique_tenant("vendorshift_none")
    now = _now()
    prior = now - timedelta(days=10)
    cur = now - timedelta(days=1)

    # PRIOR: Anthropic $6 (2M Sonnet), OpenAI $2.50 (1M gpt-4o) -> A≈71%.
    _seed_usage_record(
        tenant, occurred_at=prior, model=_SONNET, input_tokens=2_000_000,
        output_tokens=0,
    )
    _seed_openai_usage(
        tenant, occurred_at=prior, model=_GPT4O, input_uncached=1_000_000
    )
    # CURRENT: Anthropic $6.60 (2.2M Sonnet), OpenAI $2.75 (1.1M gpt-4o)
    # -> A≈71% (≈0pp move), each vendor < 3x. Per-model mix also stable.
    _seed_usage_record(
        tenant, occurred_at=cur, model=_SONNET, input_tokens=2_200_000,
        output_tokens=0,
    )
    _seed_openai_usage(
        tenant, occurred_at=cur, model=_GPT4O, input_uncached=1_100_000
    )

    card = build_card(tenant, "7d")

    assert _vendor_items(card) == []
    # Per-model mix is stable too → the whole card is idle.
    assert card.severity == "idle"
    assert card.findings_count == 0


def test_within_vendor_shift_openai_mini_to_4o(clean_records: None) -> None:
    """The within-vendor model-shift signal works for OpenAI too: a
    gpt-4o-mini -> gpt-4o migration (a per-turn cost multiplier) is
    flagged as a within-vendor model finding keyed 'OpenAI / <model>'.

    Single vendor (OpenAI) in both windows, so NO vendor-mix finding — the
    finding must be a model line, proving the within-vendor path spans
    OpenAI's models."""
    from vargate_telemetry.insights.cards.model_mix import build_card

    tenant = _unique_tenant("within_openai")
    now = _now()

    # PRIOR: gpt-4o-mini only. CURRENT: gpt-4o only (a 100pp model swing
    # within OpenAI).
    _seed_openai_usage(
        tenant, occurred_at=now - timedelta(days=10), model=_GPT4O_MINI,
        input_uncached=10_000_000,
    )
    _seed_openai_usage(
        tenant, occurred_at=now - timedelta(days=1), model=_GPT4O,
        input_uncached=1_000_000,
    )

    card = build_card(tenant, "7d")

    assert card.severity == "advisory"
    # Single vendor → no vendor-mix finding.
    assert _vendor_items(card) == []
    # A within-vendor model finding, keyed "OpenAI / <model>".
    model_items = [
        it for it in card.items if it.label.startswith("OpenAI / ")
    ]
    assert len(model_items) >= 1, [it.label for it in card.items]
    assert any("->" in (it.detail or "") for it in model_items)


def test_within_vendor_anthropic_shift_preserved_cross_vendor(
    clean_records: None,
) -> None:
    """The TM7 Sonnet->Opus within-vendor signal is preserved even when a
    second vendor (OpenAI) carries steady spend in both windows.

    OpenAI is identical across both weeks (no vendor pivot, no OpenAI model
    shift); Anthropic swings Sonnet->Opus. The Opus model line must still
    fire, keyed 'Anthropic / <opus>'."""
    from vargate_telemetry.insights.cards.model_mix import build_card

    tenant = _unique_tenant("within_anthropic_xv")
    now = _now()
    prior = now - timedelta(days=10)
    cur = now - timedelta(days=1)

    # Anthropic: Sonnet (prior) -> Opus (current).
    _seed_usage_record(tenant, occurred_at=prior, model=_SONNET)
    _seed_usage_record(tenant, occurred_at=cur, model=_OPUS)
    # OpenAI: same gpt-4o spend in BOTH windows (steady; no shift).
    _seed_openai_usage(
        tenant, occurred_at=prior, model=_GPT4O, input_uncached=1_000_000
    )
    _seed_openai_usage(
        tenant, occurred_at=cur, model=_GPT4O, input_uncached=1_000_000
    )

    card = build_card(tenant, "7d")

    assert card.severity == "advisory"
    opus_items = [
        it for it in card.items
        if it.label.startswith("Anthropic / ") and "opus" in it.label.lower()
    ]
    assert len(opus_items) >= 1, [it.label for it in card.items]
