# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for budget spend computation (TM3 Phase B2).

Seeds synthetic ``telemetry_records`` (admin-usage shape, with
breakdown rows in metadata.results) and exercises
:func:`vargate_telemetry.budgets.spend.compute_spend_in_window` for
each scope kind + the supersession filter.

Why this matters: this helper is the SAME function the dashboard
detail endpoint AND the alert evaluator both call. A drift would
mean the UI shows 71% but the evaluator decides to fire on 70%, or
vice-versa — confusing customers and (worse) leaving them without
the alert their budget should have produced.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterator

import pytest
from sqlalchemy import text as sql_text

from vargate_telemetry.budgets.spend import compute_spend_in_window
from vargate_telemetry.db import engine, session_scope


# ───────────────────────────────────────────────────────────────────────────
# Fixtures + helpers
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_records() -> Iterator[None]:
    """Empty telemetry_records before AND after — same shape as
    test_sessions_api.py's fixture so RLS-isolation tests aren't
    confused by residue from a previous case."""
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
    results: list[dict],
) -> None:
    """Insert one admin-usage record with the given breakdown rows.

    ``results`` is the list that goes into ``metadata.results[]`` —
    each element is a UsageBreakdown-shaped dict (model,
    workspace_id, api_key_id, *_tokens). The supersession filter
    keys off ``model`` being null vs. present on each result.
    """
    md = {
        "starting_at": occurred_at.isoformat(),
        "ending_at": (
            occurred_at.replace(hour=23, minute=59, second=59)
        ).isoformat(),
        "results": results,
    }
    eid = (
        f"usage:{occurred_at.date().isoformat()}:"
        f"{occurred_at.date().isoformat()}:"
        f"{results[0].get('model', '-')}:"
        f"{results[0].get('workspace_id', '-')}:"
        f"{results[0].get('api_key_id', '-')}"
    )
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


# Sonnet 4.5: input $3 / Mtok, output $15 / Mtok. Round-numbered token
# counts are picked so the expected USD math is exact.
_SONNET = "claude-sonnet-4-5-20250929"
# 1M input + 200k output → 1.0 * $3.00 + 0.2 * $15.00 = $6.00
_TOKENS_6USD: dict = {
    "model": _SONNET,
    "workspace_id": None,
    "api_key_id": None,
    "input_tokens": 1_000_000,
    "output_tokens": 200_000,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
}


# ───────────────────────────────────────────────────────────────────────────
# Cases
# ───────────────────────────────────────────────────────────────────────────


def test_empty_window_returns_zero(clean_records: None) -> None:
    with session_scope("tnt_us_spend_empty") as s:
        spend = compute_spend_in_window(
            s,
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 1, tzinfo=timezone.utc),
            scope_kind="tenant",
            scope_value=None,
        )
    assert spend == Decimal("0.00")


def test_single_record_in_window_returns_known_cost(
    clean_records: None,
) -> None:
    tenant = "tnt_us_spend_single"
    _seed_usage_record(
        tenant,
        occurred_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        results=[_TOKENS_6USD],
    )

    with session_scope(tenant) as s:
        spend = compute_spend_in_window(
            s,
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 1, tzinfo=timezone.utc),
            scope_kind="tenant",
            scope_value=None,
        )
    assert spend == Decimal("6.00")


def test_record_outside_window_is_excluded(clean_records: None) -> None:
    tenant = "tnt_us_spend_outside"
    # Same tokens, but the record falls outside the May window.
    _seed_usage_record(
        tenant,
        occurred_at=datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
        results=[_TOKENS_6USD],
    )

    with session_scope(tenant) as s:
        spend = compute_spend_in_window(
            s,
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 1, tzinfo=timezone.utc),
            scope_kind="tenant",
            scope_value=None,
        )
    assert spend == Decimal("0.00")


def test_scope_workspace_filters_to_matching_workspace(
    clean_records: None,
) -> None:
    tenant = "tnt_us_spend_workspace"
    # Workspace A spends $6, workspace B also spends $6. The
    # workspace-A budget should see only A's $6.
    _seed_usage_record(
        tenant,
        occurred_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        results=[{**_TOKENS_6USD, "workspace_id": "ws_a"}],
    )
    _seed_usage_record(
        tenant,
        occurred_at=datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc),
        results=[{**_TOKENS_6USD, "workspace_id": "ws_b"}],
    )

    with session_scope(tenant) as s:
        spend_a = compute_spend_in_window(
            s,
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 1, tzinfo=timezone.utc),
            scope_kind="workspace",
            scope_value="ws_a",
        )
        spend_b = compute_spend_in_window(
            s,
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 1, tzinfo=timezone.utc),
            scope_kind="workspace",
            scope_value="ws_b",
        )
    assert spend_a == Decimal("6.00")
    assert spend_b == Decimal("6.00")


def test_scope_api_key_filters_to_matching_key(
    clean_records: None,
) -> None:
    tenant = "tnt_us_spend_api_key"
    _seed_usage_record(
        tenant,
        occurred_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        results=[{**_TOKENS_6USD, "api_key_id": "apikey_prod"}],
    )
    _seed_usage_record(
        tenant,
        occurred_at=datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc),
        results=[{**_TOKENS_6USD, "api_key_id": "apikey_ci"}],
    )

    with session_scope(tenant) as s:
        spend = compute_spend_in_window(
            s,
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 1, tzinfo=timezone.utc),
            scope_kind="api_key",
            scope_value="apikey_prod",
        )
    assert spend == Decimal("6.00")


def test_scope_model_filters_to_matching_model(
    clean_records: None,
) -> None:
    tenant = "tnt_us_spend_model"
    _seed_usage_record(
        tenant,
        occurred_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        results=[_TOKENS_6USD],  # sonnet
    )
    _seed_usage_record(
        tenant,
        occurred_at=datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc),
        results=[{**_TOKENS_6USD, "model": "claude-haiku-4-5"}],
    )

    with session_scope(tenant) as s:
        spend = compute_spend_in_window(
            s,
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 1, tzinfo=timezone.utc),
            scope_kind="model",
            scope_value=_SONNET,
        )
    assert spend == Decimal("6.00")


def test_unknown_model_contributes_zero(clean_records: None) -> None:
    tenant = "tnt_us_spend_unknown_model"
    _seed_usage_record(
        tenant,
        occurred_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        results=[{**_TOKENS_6USD, "model": "claude-future-not-priced"}],
    )

    with session_scope(tenant) as s:
        spend = compute_spend_in_window(
            s,
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 1, tzinfo=timezone.utc),
            scope_kind="tenant",
            scope_value=None,
        )
    # No fake number — the row contributes zero rather than a
    # plausible-but-wrong dollar figure.
    assert spend == Decimal("0.00")


def test_supersession_filter_hides_legacy_aggregate_when_per_model_exists(
    clean_records: None,
) -> None:
    """Two records on the same UTC date:
      - a legacy aggregate (model=null) with the full daily total
      - a per-model breakdown with the real attribution

    Without supersession, the spend would double — once for the
    aggregate and once for the breakdown. With supersession, the
    aggregate is hidden by the breakdown's presence and we see only
    the per-model spend.
    """
    tenant = "tnt_us_spend_supersession"
    legacy_aggregate: dict = {
        "model": None,
        "workspace_id": None,
        "api_key_id": None,
        # The legacy bucket carries the full daily total.
        "input_tokens": 1_000_000,
        "output_tokens": 200_000,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    _seed_usage_record(
        tenant,
        occurred_at=datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc),
        results=[legacy_aggregate],
    )
    # Same UTC date — supersession hides the legacy aggregate.
    _seed_usage_record(
        tenant,
        occurred_at=datetime(2026, 5, 10, 0, 0, 1, tzinfo=timezone.utc),
        results=[_TOKENS_6USD],
    )

    with session_scope(tenant) as s:
        spend = compute_spend_in_window(
            s,
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 1, tzinfo=timezone.utc),
            scope_kind="tenant",
            scope_value=None,
        )
    # Just the per-model row's $6.00 — the legacy aggregate is
    # hidden by supersession, so no double-count.
    assert spend == Decimal("6.00")


def test_scope_value_validation_rejects_bad_pairs() -> None:
    """The helper guards against malformed scope/value combinations
    even though the DB CHECK constraint would also catch them at
    insert time. Defense in depth."""
    with session_scope("tnt_us_spend_validation") as s:
        # api_key scope without a value → ValueError.
        with pytest.raises(ValueError, match="non-empty scope_value"):
            compute_spend_in_window(
                s,
                start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                end=datetime(2026, 6, 1, tzinfo=timezone.utc),
                scope_kind="api_key",
                scope_value=None,
            )
        # tenant scope WITH a value → ValueError.
        with pytest.raises(
            ValueError, match="scope_kind='tenant' must have scope_value=None"
        ):
            compute_spend_in_window(
                s,
                start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                end=datetime(2026, 6, 1, tzinfo=timezone.utc),
                scope_kind="tenant",
                scope_value="something",
            )
