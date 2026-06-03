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
