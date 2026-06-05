# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the cross-vendor spend accessors (TM8 Phase D).

Exercises the additive per-vendor layer in
``vargate_telemetry.insights.spend_data``:

  - :func:`vendor_spend_breakdown` — the ``vendor -> VendorSpend`` split.
  - :func:`vendor_daily_spend` — the per-vendor daily series.

What's pinned:
  - **Per-vendor split** — Anthropic admin usage + OpenAI usage records
    in the same tenant roll up into separate vendor buckets with the
    right totals.
  - **Anthropic basis = estimated**, and its total equals the
    Anthropic-only :func:`daily_spend` for the same window
    (regression-safe: the new accessor doesn't change Anthropic's
    number).
  - **OpenAI prefers authoritative /costs** — when the
    ``openai_admin_costs`` stream has billed amounts, the OpenAI figure
    is the SUM of ``amount_value`` with ``basis="authoritative"``, NOT
    the usage estimate.
  - **OpenAI falls back to usage estimate** with ``basis="estimated"``
    when no costs stream exists.
  - **Empty tenant → {}** (sparse; no $0 entries).

Seeds synthetic ``telemetry_records`` directly (same raw-SQL pattern as
``test_card_cost_forecasting`` / ``test_insights_forecast``), shaping
metadata exactly as the pull tasks write it. Calls the accessors
directly — no HTTP.

Dates are RELATIVE to now so the trailing-window arithmetic holds
regardless of the calendar day the suite runs on.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterator

import pytest
from sqlalchemy import text as sql_text

os.environ.setdefault(
    "JWT_SIGNING_KEY",
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b",
)

# Sonnet input $3/MTok; gpt-4o input $2.50/MTok, cached $1.25, output $10.
_SONNET = "claude-sonnet-4-5-20250929"
_GPT4O = "gpt-4o"

_WINDOW = 14


# ───────────────────────────────────────────────────────────────────────────
# Fixtures + helpers
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_records() -> Iterator[None]:
    """Empty telemetry_records before AND after each test."""
    from vargate_telemetry.db import engine

    def _truncate() -> None:
        with engine.begin() as conn:
            conn.execute(
                sql_text(
                    "TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE"
                )
            )

    _truncate()
    yield
    _truncate()


def _tid(name: str) -> str:
    return f"tnt_us_{name}_" + uuid.uuid4().hex[:8]


def _provision_tenant(tenant_id: str, region: str = "us") -> None:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO tenants (tenant_id, region, active, billing_status)
                VALUES (:t, :r, TRUE, 'trial')
                ON CONFLICT (tenant_id) DO NOTHING
                """
            ),
            {"t": tenant_id, "r": region},
        )


def _insert_record(
    tenant_id: str,
    *,
    record_type: str,
    source_api: str,
    occurred_at: datetime,
    metadata: dict,
) -> None:
    """Insert one telemetry_records row with the chain columns stubbed.

    The accessors only read ``source_api`` / ``record_type`` /
    ``occurred_at`` / ``metadata`` — the chain hashes are irrelevant to
    spend math, so we stub them like the sibling insights tests do."""
    from vargate_telemetry.db import engine

    eid = f"{source_api}:{uuid.uuid4()}"
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, :rtype, :src, :eid,
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
                "rtype": record_type,
                "src": source_api,
                "eid": eid,
                "occurred_at": occurred_at,
                "metadata": json.dumps(metadata),
                "zero32": "00" * 32,
                "one32": "11" * 32,
            },
        )


def _seed_anthropic_usage(
    tenant_id: str,
    *,
    occurred_at: datetime,
    input_tokens: int,
    output_tokens: int = 0,
    model: str | None = _SONNET,
) -> None:
    """Anthropic admin usage record (pull_admin metadata shape)."""
    md = {
        "starting_at": occurred_at.isoformat(),
        "ending_at": occurred_at.isoformat(),
        "results": [
            {
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }
        ],
    }
    _insert_record(
        tenant_id,
        record_type="usage",
        source_api="admin",
        occurred_at=occurred_at,
        metadata=md,
    )


def _seed_openai_usage(
    tenant_id: str,
    *,
    occurred_at: datetime,
    input_uncached: int,
    input_cached: int = 0,
    output: int = 0,
    model: str | None = _GPT4O,
) -> None:
    """OpenAI usage record (pull_openai_usage metadata shape)."""
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
        "subject_user_id": "user-oai",
    }
    _insert_record(
        tenant_id,
        record_type="usage",
        source_api="openai_admin_usage",
        occurred_at=occurred_at,
        metadata=md,
    )


def _seed_openai_cost(
    tenant_id: str,
    *,
    occurred_at: datetime,
    amount_value: str,
    line_item: str = "gpt-4o-2024-08-06, input",
) -> None:
    """OpenAI authoritative cost record (pull_openai_costs metadata shape)."""
    md = {
        "start_time": occurred_at.isoformat(),
        "end_time": occurred_at.isoformat(),
        "result": {"amount": {"value": amount_value, "currency": "usd"}},
        "line_item": line_item,
        "project_id": "proj_alpha",
        "project_name": "Alpha",
        "amount_value": amount_value,
        "currency": "usd",
    }
    _insert_record(
        tenant_id,
        record_type="cost",
        source_api="openai_admin_costs",
        occurred_at=occurred_at,
        metadata=md,
    )


def _recent(days_ago: int) -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(days=days_ago)


# ───────────────────────────────────────────────────────────────────────────
# vendor_spend_breakdown — the per-vendor split
# ───────────────────────────────────────────────────────────────────────────


def test_per_vendor_split_estimated_both(clean_records: None) -> None:
    """Anthropic admin + OpenAI usage in one tenant roll up to separate
    vendor buckets with the right totals, both ``estimated``.

    Anthropic: 2M Sonnet input = $6.00. OpenAI: 1M gpt-4o uncached =
    $2.50."""
    from vargate_telemetry.insights import spend_data
    from vargate_telemetry.pricing.vendor_cost import (
        VENDOR_ANTHROPIC,
        VENDOR_OPENAI,
    )

    tenant = _tid("split_est")
    _provision_tenant(tenant)
    _seed_anthropic_usage(
        tenant, occurred_at=_recent(1), input_tokens=2_000_000
    )
    _seed_openai_usage(
        tenant, occurred_at=_recent(1), input_uncached=1_000_000
    )

    breakdown = spend_data.vendor_spend_breakdown(tenant, _WINDOW)

    assert set(breakdown) == {VENDOR_ANTHROPIC, VENDOR_OPENAI}
    assert breakdown[VENDOR_ANTHROPIC].usd == Decimal("6.00")
    assert breakdown[VENDOR_ANTHROPIC].basis == spend_data.BASIS_ESTIMATED
    assert breakdown[VENDOR_OPENAI].usd == Decimal("2.50")
    assert breakdown[VENDOR_OPENAI].basis == spend_data.BASIS_ESTIMATED


def test_anthropic_total_equals_daily_spend(clean_records: None) -> None:
    """The new accessor's Anthropic figure equals the Anthropic-only
    :func:`daily_spend` total over the same window — regression-safe (the
    cross-vendor path must not move Anthropic's number)."""
    from vargate_telemetry.insights import spend_data
    from vargate_telemetry.pricing.vendor_cost import VENDOR_ANTHROPIC

    tenant = _tid("anthropic_parity")
    _provision_tenant(tenant)
    # A few distinct days of Sonnet usage.
    for k in range(1, 5):
        _seed_anthropic_usage(
            tenant,
            occurred_at=_recent(k),
            input_tokens=1_000_000 * k,
            output_tokens=0,
        )

    series = spend_data.daily_spend(tenant, _WINDOW)
    daily_total = sum((u for _, u in series), Decimal("0")).quantize(
        Decimal("0.01")
    )

    breakdown = spend_data.vendor_spend_breakdown(tenant, _WINDOW)
    assert breakdown[VENDOR_ANTHROPIC].usd == daily_total


def test_openai_prefers_authoritative_costs(clean_records: None) -> None:
    """When the openai_admin_costs stream has billed amounts, the OpenAI
    figure is the SUM of ``amount_value`` (authoritative), NOT the usage
    estimate — and the basis says so.

    Seed usage that would ESTIMATE $2.50, plus a /costs record billing an
    authoritative $9.99. The breakdown must report $9.99 authoritative."""
    from vargate_telemetry.insights import spend_data
    from vargate_telemetry.pricing.vendor_cost import VENDOR_OPENAI

    tenant = _tid("oai_auth")
    _provision_tenant(tenant)
    _seed_openai_usage(
        tenant, occurred_at=_recent(1), input_uncached=1_000_000
    )  # would estimate $2.50
    _seed_openai_cost(
        tenant, occurred_at=_recent(1), amount_value="9.99"
    )

    breakdown = spend_data.vendor_spend_breakdown(tenant, _WINDOW)

    assert breakdown[VENDOR_OPENAI].usd == Decimal("9.99")
    assert breakdown[VENDOR_OPENAI].basis == spend_data.BASIS_AUTHORITATIVE
    # Specifically NOT the usage estimate.
    assert breakdown[VENDOR_OPENAI].usd != Decimal("2.50")


def test_openai_costs_sum_across_days(clean_records: None) -> None:
    """Authoritative OpenAI spend sums billed amounts across multiple
    days / line items."""
    from vargate_telemetry.insights import spend_data
    from vargate_telemetry.pricing.vendor_cost import VENDOR_OPENAI

    tenant = _tid("oai_costs_sum")
    _provision_tenant(tenant)
    _seed_openai_cost(tenant, occurred_at=_recent(1), amount_value="4.00")
    _seed_openai_cost(
        tenant,
        occurred_at=_recent(1),
        amount_value="1.50",
        line_item="gpt-4o-2024-08-06, output",
    )
    _seed_openai_cost(tenant, occurred_at=_recent(3), amount_value="2.25")

    breakdown = spend_data.vendor_spend_breakdown(tenant, _WINDOW)
    assert breakdown[VENDOR_OPENAI].usd == Decimal("7.75")
    assert breakdown[VENDOR_OPENAI].basis == spend_data.BASIS_AUTHORITATIVE


def test_openai_falls_back_to_estimate(clean_records: None) -> None:
    """With no /costs records, OpenAI spend falls back to the usage-token
    estimate and the basis reads ``estimated``."""
    from vargate_telemetry.insights import spend_data
    from vargate_telemetry.pricing.vendor_cost import VENDOR_OPENAI

    tenant = _tid("oai_estimate")
    _provision_tenant(tenant)
    _seed_openai_usage(
        tenant,
        occurred_at=_recent(1),
        input_uncached=1_000_000,
        input_cached=1_000_000,  # cached @ $1.25 -> 1.25
        output=100_000,  # @ $10 -> 1.00
    )

    breakdown = spend_data.vendor_spend_breakdown(tenant, _WINDOW)
    # 2.50 + 1.25 + 1.00 = 4.75
    assert breakdown[VENDOR_OPENAI].usd == Decimal("4.75")
    assert breakdown[VENDOR_OPENAI].basis == spend_data.BASIS_ESTIMATED


def test_empty_tenant_returns_empty_map(clean_records: None) -> None:
    """A tenant with no usage/cost records → ``{}`` (sparse; no $0
    vendor entries)."""
    from vargate_telemetry.insights import spend_data

    tenant = _tid("empty")
    _provision_tenant(tenant)

    assert spend_data.vendor_spend_breakdown(tenant, _WINDOW) == {}
    assert spend_data.vendor_daily_spend(tenant, _WINDOW) == {}


def test_openai_empty_cost_sentinel_does_not_count(
    clean_records: None,
) -> None:
    """An empty-bucket cost sentinel (``amount_value=null``) must NOT
    count as authoritative spend — with only a sentinel + usage, the
    OpenAI figure falls back to the usage estimate."""
    from vargate_telemetry.insights import spend_data
    from vargate_telemetry.pricing.vendor_cost import VENDOR_OPENAI

    tenant = _tid("oai_sentinel")
    _provision_tenant(tenant)
    # Cost sentinel — no amount_value (the empty-bucket record shape).
    _insert_record(
        tenant,
        record_type="cost",
        source_api="openai_admin_costs",
        occurred_at=_recent(1),
        metadata={
            "start_time": _recent(1).isoformat(),
            "end_time": _recent(1).isoformat(),
            "result": None,
        },
    )
    _seed_openai_usage(
        tenant, occurred_at=_recent(1), input_uncached=1_000_000
    )

    breakdown = spend_data.vendor_spend_breakdown(tenant, _WINDOW)
    assert breakdown[VENDOR_OPENAI].usd == Decimal("2.50")
    assert breakdown[VENDOR_OPENAI].basis == spend_data.BASIS_ESTIMATED


# ───────────────────────────────────────────────────────────────────────────
# vendor_daily_spend — the per-vendor series
# ───────────────────────────────────────────────────────────────────────────


def test_vendor_daily_spend_series(clean_records: None) -> None:
    """Per-vendor daily series carry one ascending entry per priceable
    day, per vendor with spend."""
    from vargate_telemetry.insights import spend_data
    from vargate_telemetry.pricing.vendor_cost import (
        VENDOR_ANTHROPIC,
        VENDOR_OPENAI,
    )

    tenant = _tid("daily")
    _provision_tenant(tenant)
    _seed_anthropic_usage(
        tenant, occurred_at=_recent(2), input_tokens=1_000_000
    )  # $3.00
    _seed_anthropic_usage(
        tenant, occurred_at=_recent(1), input_tokens=2_000_000
    )  # $6.00
    _seed_openai_cost(tenant, occurred_at=_recent(1), amount_value="5.00")

    daily = spend_data.vendor_daily_spend(tenant, _WINDOW)

    assert set(daily) == {VENDOR_ANTHROPIC, VENDOR_OPENAI}
    # Anthropic: two distinct days, ascending by date.
    anthropic = daily[VENDOR_ANTHROPIC]
    assert len(anthropic) == 2
    assert anthropic[0][0] < anthropic[1][0]
    assert sum((u for _, u in anthropic), Decimal("0")) == Decimal("9.00")
    # OpenAI authoritative: one day at $5.00.
    openai = daily[VENDOR_OPENAI]
    assert len(openai) == 1
    assert openai[0][1] == Decimal("5.00")


def test_vendor_daily_spend_openai_estimate_when_no_costs(
    clean_records: None,
) -> None:
    """vendor_daily_spend uses the OpenAI usage estimate when /costs is
    absent — consistent with vendor_spend_breakdown's fallback."""
    from vargate_telemetry.insights import spend_data
    from vargate_telemetry.pricing.vendor_cost import VENDOR_OPENAI

    tenant = _tid("daily_est")
    _provision_tenant(tenant)
    _seed_openai_usage(
        tenant, occurred_at=_recent(1), input_uncached=1_000_000
    )

    daily = spend_data.vendor_daily_spend(tenant, _WINDOW)
    assert VENDOR_OPENAI in daily
    assert daily[VENDOR_OPENAI][0][1] == Decimal("2.50")
