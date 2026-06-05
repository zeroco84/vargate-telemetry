# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the cache-efficiency Insights card (TM7).

Exercises ``vargate_telemetry.insights.cards.cache_efficiency.build_card``
directly (no HTTP) over a 7-day window:

  - A model with POOR cache reuse (lots of cache *creation*, almost no
    cache *read*, on a large input volume) escalates the card to a
    loud severity and surfaces a per-model finding whose detail names
    the hit ratio.
  - A model with HEALTHY reuse (read >> creation, hit ratio ≥ 0.8) is
    not flagged at all — the card falls back to its idle empty-state.

Each test uses a unique ``tenant_id`` and TRUNCATEs ``telemetry_records``
before + after so the per-tenant ``SUM`` the card runs is deterministic.
The usage rows are seeded exactly like ``test_usage_api.py`` /
``test_budgets_api.py``: ``record_type='usage'``, ``source_api='admin'``,
a ``metadata.results`` array of per-model breakdowns, ``chain_seq`` from
``COALESCE(MAX+1)``, and content/prev/self hashes as ``decode`` of hex.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Iterator

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
def clean_records() -> Iterator[None]:
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


def _seed_usage_record(
    tenant_id: str,
    *,
    occurred_at: datetime,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    workspace_id: str | None = None,
) -> None:
    """Insert one ``record_type='usage'`` row with a single result group.

    Mirrors the Admin API connector's output shape (a
    ``metadata.results`` JSONB array) and the seed helpers in
    ``test_usage_api.py`` / ``test_budgets_api.py``. ``occurred_at``
    decides which trailing-day window the row lands in — pass an
    earlier value to push a record into a prior window.
    """
    from vargate_telemetry.db import engine

    results = [
        {
            "model": model,
            "workspace_id": workspace_id,
            "api_key_id": None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
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


def _seed_openai_usage_record(
    tenant_id: str,
    *,
    occurred_at: datetime,
    model: str,
    input_uncached: int = 0,
    input_cached: int = 0,
    output: int = 0,
) -> None:
    """Insert one OpenAI usage record (pull_openai_usage metadata shape).

    The OpenAI cache analysis reads the per-model uncached / cached input
    split from ``metadata->'result'``. OpenAI caching is automatic and
    input-only (no cache-creation), so there's no cache_creation field.
    """
    from vargate_telemetry.db import engine

    md = {
        "start_time": occurred_at.isoformat(),
        "end_time": occurred_at.isoformat(),
        "modality": "completions",
        "result": {
            "model": model,
            "input_tokens": input_uncached + input_cached,
            "input_uncached_tokens": input_uncached,
            "input_cached_tokens": input_cached,
            "output_tokens": output,
        },
        "model": model,
        "subject_user_id": "user-oai",
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
                       FROM telemetry_records WHERE tenant_id = :t_lookup),
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


def _tenant(name: str) -> str:
    """Unique per-test tenant id."""
    return f"tnt_us_{name}_" + uuid.uuid4().hex[:8]


# ───────────────────────────────────────────────────────────────────────────
# (a) Poor cache reuse → loud finding
# ───────────────────────────────────────────────────────────────────────────


def test_build_card_flags_poor_cache_reuse(clean_records: None) -> None:
    """A model that writes a lot of cache but barely reads it back, on a
    large input volume, escalates the card and surfaces a finding whose
    detail names the hit ratio."""
    from vargate_telemetry.insights.cards.cache_efficiency import build_card

    tenant = _tenant("cache_poor")
    model = "claude-poor-cache-model"

    # cache_read (10k) << cache_creation (900k) → hit ratio ≈ 1.1%, well
    # under the <0.30 "action" floor. input_tokens (200k) keeps total
    # input above the 100k volume floor so the verdict isn't an "ok"
    # low-volume pass.
    _seed_usage_record(
        tenant,
        occurred_at=datetime.now(timezone.utc),
        model=model,
        input_tokens=200_000,
        output_tokens=50_000,
        cache_read_input_tokens=10_000,
        cache_creation_input_tokens=900_000,
    )

    card = build_card(tenant, "7d")

    assert card.severity in ("warning", "action")
    assert card.findings_count >= 1

    matching = [it for it in card.items if it.label == model]
    assert matching, f"expected a finding labelled {model!r}; got {card.items}"
    assert matching[0].detail is not None
    assert "hit ratio" in matching[0].detail


# ───────────────────────────────────────────────────────────────────────────
# (b) Healthy cache reuse → idle empty-state
# ───────────────────────────────────────────────────────────────────────────


def test_build_card_idle_when_cache_reuse_is_healthy(
    clean_records: None,
) -> None:
    """A model with read >> creation (hit ratio ≥ 0.8) is not flagged —
    the card returns idle with no findings and an empty-state string."""
    from vargate_telemetry.insights.cards.cache_efficiency import build_card

    tenant = _tenant("cache_healthy")

    # cache_read (900k) >> cache_creation (100k) → hit ratio 0.9. Above
    # the 100k volume floor, so this is a real "healthy reuse" verdict,
    # not a low-volume skip.
    _seed_usage_record(
        tenant,
        occurred_at=datetime.now(timezone.utc),
        model="claude-healthy-cache-model",
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=900_000,
        cache_creation_input_tokens=100_000,
    )

    card = build_card(tenant, "7d")

    assert card.severity == "idle"
    assert card.findings_count == 0
    assert card.items == []
    assert card.empty_state


# ───────────────────────────────────────────────────────────────────────────
# Cross-vendor (TM8 Phase D) — OpenAI automatic-caching recommendations
# ───────────────────────────────────────────────────────────────────────────


def test_openai_prefix_below_caching_minimum_is_flagged(
    clean_records: None,
) -> None:
    """An OpenAI model with lots of uncached input and ~no cached input
    (the 'prompt prefix below the ~1024-token automatic-caching minimum'
    case) is flagged as a warning, vendor-tagged, with the cached-input
    fraction in the detail."""
    from vargate_telemetry.insights.cards.cache_efficiency import build_card

    tenant = _tenant("oai_warn")
    model = "gpt-4o"

    # 500k uncached, 0 cached → cached fraction 0%, above the 100k volume
    # floor → "verify your prefix" warning.
    _seed_openai_usage_record(
        tenant,
        occurred_at=datetime.now(timezone.utc),
        model=model,
        input_uncached=500_000,
        input_cached=0,
        output=50_000,
    )

    card = build_card(tenant, "7d")

    assert card.severity in ("warning", "action")
    assert card.findings_count >= 1
    matching = [it for it in card.items if it.label == model]
    assert matching, f"expected a finding labelled {model!r}; got {card.items}"
    assert matching[0].detail is not None
    assert "OpenAI" in matching[0].detail
    assert "cached" in matching[0].detail
    # gpt-4o is priceable → an upper-bound "if cached" saving is shown.
    assert matching[0].value is not None
    assert "if cached" in matching[0].value


def test_openai_healthy_caching_not_flagged(clean_records: None) -> None:
    """An OpenAI model already caching a healthy fraction of its input
    (>= 30%) is not flagged — on an OpenAI-only tenant the card is idle."""
    from vargate_telemetry.insights.cards.cache_efficiency import build_card

    tenant = _tenant("oai_healthy")

    # 400k uncached + 600k cached → 60% cached, above the healthy floor.
    _seed_openai_usage_record(
        tenant,
        occurred_at=datetime.now(timezone.utc),
        model="gpt-4o",
        input_uncached=400_000,
        input_cached=600_000,
        output=10_000,
    )

    card = build_card(tenant, "7d")

    assert card.severity == "idle"
    assert card.findings_count == 0
    assert card.items == []
    assert card.empty_state


def test_openai_low_volume_not_flagged(clean_records: None) -> None:
    """Below the 100k input volume floor, an OpenAI model isn't flagged —
    automatic caching has too little signal to reason about yet."""
    from vargate_telemetry.insights.cards.cache_efficiency import build_card

    tenant = _tenant("oai_lowvol")

    _seed_openai_usage_record(
        tenant,
        occurred_at=datetime.now(timezone.utc),
        model="gpt-4o",
        input_uncached=10_000,  # well under 100k
        input_cached=0,
    )

    card = build_card(tenant, "7d")
    assert card.severity == "idle"
    assert card.findings_count == 0


def test_both_vendors_headline_and_findings(clean_records: None) -> None:
    """A tenant with BOTH an Anthropic poor-reuse model and an OpenAI
    prefix-too-short model surfaces findings for each (vendor-tagged) and
    a headline that says the recommendations span both vendors."""
    from vargate_telemetry.insights.cards.cache_efficiency import build_card

    tenant = _tenant("xvendor_cache")
    now = datetime.now(timezone.utc)

    # Anthropic: poor reuse (read 10k << creation 900k, ~1% hit ratio).
    _seed_usage_record(
        tenant,
        occurred_at=now,
        model="claude-poor-cache-model",
        input_tokens=200_000,
        output_tokens=50_000,
        cache_read_input_tokens=10_000,
        cache_creation_input_tokens=900_000,
    )
    # OpenAI: prefix-too-short (500k uncached, 0 cached).
    _seed_openai_usage_record(
        tenant,
        occurred_at=now,
        model="gpt-4o",
        input_uncached=500_000,
        input_cached=0,
        output=50_000,
    )

    card = build_card(tenant, "7d")

    assert card.findings_count >= 2
    labels = [it.label for it in card.items]
    assert "claude-poor-cache-model" in labels
    assert "gpt-4o" in labels
    # Each finding names its vendor.
    anthropic_item = next(
        it for it in card.items if it.label == "claude-poor-cache-model"
    )
    openai_item = next(it for it in card.items if it.label == "gpt-4o")
    assert "Anthropic" in (anthropic_item.detail or "")
    assert "OpenAI" in (openai_item.detail or "")
    # Headline reflects the cross-vendor mix.
    assert "both vendors" in card.headline


def test_anthropic_only_analysis_unchanged(clean_records: None) -> None:
    """An Anthropic-only tenant's findings are materially unchanged from
    TM7: same model flagged, the hit-ratio detail, and a recoverable-
    premium value. (The detail now carries an 'Anthropic ·' vendor tag —
    the analysis itself is identical.)"""
    from vargate_telemetry.insights.cards.cache_efficiency import build_card

    tenant = _tenant("anthropic_only_cache")
    model = "claude-poor-cache-model"

    _seed_usage_record(
        tenant,
        occurred_at=datetime.now(timezone.utc),
        model=model,
        input_tokens=200_000,
        output_tokens=50_000,
        cache_read_input_tokens=10_000,
        cache_creation_input_tokens=900_000,
    )

    card = build_card(tenant, "7d")

    assert card.severity in ("warning", "action")
    assert card.findings_count == 1
    item = card.items[0]
    assert item.label == model
    assert item.detail is not None
    assert "hit ratio" in item.detail
    assert "Anthropic" in item.detail
    # No OpenAI rows → headline is the single-vendor form, not "both".
    assert "both vendors" not in card.headline
