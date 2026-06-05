# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the cost-forecasting Insights card (TM7).

Exercises ``insights.cards.cost_forecasting.build_card`` across its
three render states plus the underlying ``insights.spend_data``
least-squares fit:

  (a) too little history (<7 distinct days) → idle, no findings,
      empty_state nudges for more data ("7 days").
  (b) ≥8 days of rising usage + a monthly tenant budget the
      projection blows past → advisory, ≥1 finding, a "Current spend"
      item, CTA pointing at the forecast detail page.
  (c) ≥8 days but no budget to compare → no findings, the empty_state
      still surfaces the projection sentence + a CTA.
  (d) ``spend_data.linear_fit`` slope sanity on a clean line.

Seeds synthetic ``telemetry_records`` (record_type=usage,
source_api=admin) the same way ``test_budgets_api`` / ``test_usage_api``
do, and inserts budgets directly under ``session_scope`` so RLS is
satisfied. ``build_card(tid, "7d")`` is called directly — no HTTP — so
these tests pin the card's logic, not the route wiring.

Dates are RELATIVE to now (``now - timedelta(days=k)``) so the
"distinct UTC days" and "month-to-date" arithmetic the forecast does
holds regardless of the calendar day the suite runs on. Today
(``k=0``) is always inside the current UTC month, so the month-to-date
spend always carries at least one day's worth — the (b) budget is set
small enough that this alone exceeds it, making the assertion robust
to the month boundary.
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


# ───────────────────────────────────────────────────────────────────────────
# Fixtures + helpers (copied from test_budgets_api / test_usage_api)
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_budgets() -> Iterator[None]:
    """Empty budgets + alert events + telemetry before AND after each test."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE budget_alert_events, budgets "
                "RESTART IDENTITY CASCADE"
            )
        )
        conn.execute(
            sql_text("TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE")
        )
    yield
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE budget_alert_events, budgets "
                "RESTART IDENTITY CASCADE"
            )
        )
        conn.execute(
            sql_text("TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE")
        )


def _tid(name: str) -> str:
    """Unique tenant id per test so leftover ``tenants`` rows can't
    collide (TRUNCATE can't always reach ``tenants`` — FKs)."""
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


# Sonnet rate: input $3/Mtok + output $15/Mtok.
# 1M input + 200k output = $6.00 — same fixture as test_budgets_api.
_SONNET = "claude-sonnet-4-5-20250929"


def _seed_usage_record(
    tenant_id: str,
    *,
    occurred_at: datetime,
    input_tokens: int = 1_000_000,
    output_tokens: int = 200_000,
    workspace_id: str | None = None,
    api_key_id: str | None = None,
    model: str | None = _SONNET,
) -> None:
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


def _insert_monthly_tenant_budget(
    tenant_id: str, *, threshold_usd: str, name: str = "monthly cap"
) -> str:
    """Insert a live monthly, tenant-scope budget directly.

    Goes through ``session_scope`` so the RLS WITH CHECK
    (``tenant_id = app.tenant_id``) is satisfied. ``created_by_user_id``
    is nullable, so we skip provisioning a users row — the forecast
    card only SELECTs ``id, name, threshold_usd``. ``alert_recipients``
    is set to an empty JSONB object (post-0023 the column is JSONB
    with the server default dropped).
    """
    from vargate_telemetry.db import session_scope

    with session_scope(tenant_id) as s:
        row = s.execute(
            sql_text(
                """
                INSERT INTO budgets (
                    tenant_id, name, scope_kind, scope_value,
                    period, threshold_usd, alert_recipients
                ) VALUES (
                    :t, :name, 'tenant', NULL,
                    'monthly', :threshold, CAST('{}' AS jsonb)
                )
                RETURNING id::text
                """
            ),
            {"t": tenant_id, "name": name, "threshold": threshold_usd},
        ).one()
    return row.id


def _seed_rising_days(tenant_id: str, num_days: int) -> None:
    """Seed ``num_days`` distinct recent UTC days of RISING usage.

    Day ``k`` (counting back from today) sits at ``now - k days`` and
    carries ``(num_days - k)`` × a base token block, so the most recent
    day has the most spend → ascending-by-day series → positive slope.
    Today (k=0) is always inside the current UTC month, so the
    month-to-date spend always picks up at least one day.
    """
    base_input = 1_000_000  # $3.00 per block at Sonnet input rate.
    for k in range(num_days):
        blocks = num_days - k  # newest day = most blocks (rising).
        _seed_usage_record(
            tenant_id,
            occurred_at=datetime.now(tz=timezone.utc) - timedelta(days=k),
            input_tokens=base_input * blocks,
            output_tokens=0,
        )


# OpenAI usage / cost seed helpers (shapes mirror pull_openai_* +
# test_vendor_spend). gpt-4o: input $2.50/MTok, cached $1.25, output $10.
_GPT4O = "gpt-4o"


def _insert_openai_record(
    tenant_id: str,
    *,
    record_type: str,
    source_api: str,
    occurred_at: datetime,
    metadata: dict,
) -> None:
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


def _seed_openai_usage(
    tenant_id: str,
    *,
    occurred_at: datetime,
    input_uncached: int,
    input_cached: int = 0,
    output: int = 0,
    model: str = _GPT4O,
) -> None:
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
    _insert_openai_record(
        tenant_id,
        record_type="usage",
        source_api="openai_admin_usage",
        occurred_at=occurred_at,
        metadata=md,
    )


def _seed_openai_cost(
    tenant_id: str, *, occurred_at: datetime, amount_value: str
) -> None:
    md = {
        "start_time": occurred_at.isoformat(),
        "end_time": occurred_at.isoformat(),
        "result": {"amount": {"value": amount_value, "currency": "usd"}},
        "line_item": "gpt-4o-2024-08-06, input",
        "project_id": "proj_alpha",
        "amount_value": amount_value,
        "currency": "usd",
    }
    _insert_openai_record(
        tenant_id,
        record_type="cost",
        source_api="openai_admin_costs",
        occurred_at=occurred_at,
        metadata=md,
    )


def _seed_openai_rising_days(tenant_id: str, num_days: int) -> None:
    """``num_days`` distinct recent UTC days of rising OpenAI usage."""
    base = 1_000_000  # $2.50/block at gpt-4o uncached-input rate.
    for k in range(num_days):
        blocks = num_days - k
        _seed_openai_usage(
            tenant_id,
            occurred_at=datetime.now(tz=timezone.utc) - timedelta(days=k),
            input_uncached=base * blocks,
        )


# ───────────────────────────────────────────────────────────────────────────
# (a) Not enough history → idle card
# ───────────────────────────────────────────────────────────────────────────


def test_idle_when_fewer_than_seven_days(clean_budgets: None) -> None:
    """Only 3 distinct days of usage → ``days_of_data < 7`` → an idle,
    finding-free card whose empty_state asks for more history (the
    "7 days" minimum is named so the operator knows the bar)."""
    from vargate_telemetry.insights.cards import cost_forecasting

    tenant = _tid("forecast_idle")
    _provision_tenant(tenant)

    # Three distinct recent UTC days.
    for k in range(3):
        _seed_usage_record(
            tenant,
            occurred_at=datetime.now(tz=timezone.utc) - timedelta(days=k),
            input_tokens=1_000_000,
            output_tokens=0,
        )

    card = cost_forecasting.build_card(tenant, "7d")

    assert card.severity == "idle"
    assert card.findings_count == 0
    assert card.empty_state is not None
    assert "7 days" in card.empty_state


# ───────────────────────────────────────────────────────────────────────────
# (b) ≥8 days, rising, with a monthly budget the projection exceeds
# ───────────────────────────────────────────────────────────────────────────


def test_advisory_when_projection_exceeds_monthly_budget(
    clean_budgets: None,
) -> None:
    """≥8 distinct rising days + a small monthly tenant cap the
    projection blows past → advisory severity, ≥1 finding, a CTA to the
    forecast detail page, and a "Current spend" item in the body.

    The cap ($1) is below even a single day's month-to-date spend ($3),
    so it is exceeded regardless of which calendar day the suite runs
    on (today is always in-month)."""
    from vargate_telemetry.insights import spend_data  # noqa: F401 (per spec)
    from vargate_telemetry.insights.cards import cost_forecasting

    tenant = _tid("forecast_advisory")
    _provision_tenant(tenant)
    _seed_rising_days(tenant, 9)  # ≥8 distinct days, rising.
    _insert_monthly_tenant_budget(tenant, threshold_usd="1.00")

    # Sanity-pin the projection actually clears the cap before asserting
    # the card shape (so a future change to seeding can't silently make
    # this a no-op idle card).
    res = spend_data.project_period_end(tenant)
    assert res.days_of_data >= 8
    assert res.projected_end > Decimal("1.00")

    card = cost_forecasting.build_card(tenant, "7d")

    assert card.severity == "advisory"
    assert card.findings_count >= 1
    assert card.cta is not None
    assert card.cta.href == "/insights/forecast"
    # The body names the current spend.
    labels = [item.label for item in card.items]
    assert "Current spend" in labels


# ───────────────────────────────────────────────────────────────────────────
# (c) ≥8 days but no budget → no findings, projection sentence + CTA
# ───────────────────────────────────────────────────────────────────────────


def test_no_budget_shows_projection_sentence_and_cta(
    clean_budgets: None,
) -> None:
    """≥8 days of usage but NO budget to compare against → the card has
    zero findings yet still surfaces a projection sentence ("on current
    pace ...") and a CTA so the operator always sees where the month is
    heading."""
    from vargate_telemetry.insights.cards import cost_forecasting

    tenant = _tid("forecast_nobudget")
    _provision_tenant(tenant)
    _seed_rising_days(tenant, 9)  # ≥8 distinct days, no budget inserted.

    card = cost_forecasting.build_card(tenant, "7d")

    assert card.findings_count == 0
    assert card.empty_state is not None
    assert "on current pace" in card.empty_state.lower()
    assert card.cta is not None


# ───────────────────────────────────────────────────────────────────────────
# (d) linear_fit slope sanity
# ───────────────────────────────────────────────────────────────────────────


def test_linear_fit_slope_on_clean_line() -> None:
    """A perfectly linear series ``y = 10x + 10`` fits to slope 10."""
    from vargate_telemetry.insights import spend_data

    slope, intercept = spend_data.linear_fit([(0.0, 10.0), (1.0, 20.0), (2.0, 30.0)])
    assert abs(slope - 10.0) < 1e-9
    assert abs(intercept - 10.0) < 1e-9


# ───────────────────────────────────────────────────────────────────────────
# (e) Cross-vendor — per-vendor projection breakdown (TM8 Phase D)
# ───────────────────────────────────────────────────────────────────────────


def test_cross_vendor_per_vendor_breakdown(clean_budgets: None) -> None:
    """A tenant with BOTH Anthropic and OpenAI spend gets a per-vendor
    breakdown in the card body, each line labelled with its basis, and
    the headline/total reflects the cross-vendor sum.

    Anthropic rising days + OpenAI rising days + a small cap the combined
    projection blows past → advisory, with one breakdown line per vendor
    (label == vendor name), each carrying a basis detail
    ("estimated"/"authoritative")."""
    from vargate_telemetry.insights.cards import cost_forecasting

    tenant = _tid("forecast_xvendor")
    _provision_tenant(tenant)
    _seed_rising_days(tenant, 9)  # Anthropic, estimated
    _seed_openai_rising_days(tenant, 9)  # OpenAI usage, estimated
    _insert_monthly_tenant_budget(tenant, threshold_usd="1.00")

    card = cost_forecasting.build_card(tenant, "7d")

    assert card.severity == "advisory"
    assert card.findings_count >= 1

    labels = [item.label for item in card.items]
    # Both vendors appear as their own breakdown lines.
    assert "Anthropic" in labels
    assert "OpenAI" in labels
    # The summary lines are still present.
    assert "Current spend" in labels
    assert "Projected end of period" in labels

    # Each vendor line names a basis and a projected figure.
    by_label = {item.label: item for item in card.items}
    for vendor in ("Anthropic", "OpenAI"):
        it = by_label[vendor]
        assert it.detail is not None
        assert "estimated" in it.detail or "authoritative" in it.detail
        assert it.value is not None and "projected" in it.value


def test_cross_vendor_headline_total_exceeds_single_vendor(
    clean_budgets: None,
) -> None:
    """The headline projection is the cross-vendor TOTAL: the combined
    projected_end is strictly greater than the Anthropic-only projection
    when OpenAI also has spend.

    Pins that the total really aggregates across vendors (the per-vendor
    sum), not just the Anthropic baseline."""
    from vargate_telemetry.insights import spend_data
    from vargate_telemetry.insights.cards import cost_forecasting

    tenant = _tid("forecast_total")
    _provision_tenant(tenant)
    _seed_rising_days(tenant, 9)
    _seed_openai_rising_days(tenant, 9)

    forecasts = cost_forecasting.vendor_forecasts(tenant)
    by_vendor = {vf.vendor: vf for vf in forecasts}
    assert set(by_vendor) == {"Anthropic", "OpenAI"}

    anthropic_only = spend_data.project_period_end(tenant).projected_end
    total = sum((vf.projected_end for vf in forecasts), Decimal("0"))

    # Anthropic's per-vendor figure equals the standalone Anthropic
    # projection (reused verbatim) ...
    assert by_vendor["Anthropic"].projected_end == anthropic_only
    # ... and OpenAI adds on top, so the cross-vendor total is larger.
    assert by_vendor["OpenAI"].projected_end > Decimal("0")
    assert total > anthropic_only


def test_openai_authoritative_basis_labeled(clean_budgets: None) -> None:
    """When the OpenAI ``/costs`` stream has billed amounts, the OpenAI
    per-vendor forecast is labelled ``authoritative`` (not ``estimated``)."""
    from vargate_telemetry.insights import spend_data
    from vargate_telemetry.insights.cards import cost_forecasting

    tenant = _tid("forecast_auth")
    _provision_tenant(tenant)
    _seed_rising_days(tenant, 9)
    # OpenAI usage (would be estimated) PLUS billed /costs over several
    # recent days → authoritative wins.
    _seed_openai_rising_days(tenant, 9)
    for k in range(9):
        _seed_openai_cost(
            tenant,
            occurred_at=datetime.now(tz=timezone.utc) - timedelta(days=k),
            amount_value="5.00",
        )

    forecasts = cost_forecasting.vendor_forecasts(tenant)
    by_vendor = {vf.vendor: vf for vf in forecasts}
    assert by_vendor["OpenAI"].basis == spend_data.BASIS_AUTHORITATIVE
    assert by_vendor["Anthropic"].basis == spend_data.BASIS_ESTIMATED


def test_single_vendor_has_no_breakdown_lines(clean_budgets: None) -> None:
    """An Anthropic-only tenant keeps the TM7 body verbatim — no
    per-vendor breakdown lines (a one-row breakdown that just restates
    the total adds nothing)."""
    from vargate_telemetry.insights.cards import cost_forecasting

    tenant = _tid("forecast_single")
    _provision_tenant(tenant)
    _seed_rising_days(tenant, 9)
    _insert_monthly_tenant_budget(tenant, threshold_usd="1.00")

    card = cost_forecasting.build_card(tenant, "7d")

    labels = [item.label for item in card.items]
    # The TM7 items, and NO vendor-named breakdown line.
    assert "Current spend" in labels
    assert "Anthropic" not in labels
    assert "OpenAI" not in labels


def test_idle_path_unchanged_with_openai_only_short_history(
    clean_budgets: None,
) -> None:
    """Fewer than 7 combined days (here OpenAI-only, 3 days) → idle, the
    same not-enough-history gate as the Anthropic path."""
    from vargate_telemetry.insights.cards import cost_forecasting

    tenant = _tid("forecast_oai_idle")
    _provision_tenant(tenant)
    _seed_openai_rising_days(tenant, 3)  # 3 distinct days only

    card = cost_forecasting.build_card(tenant, "7d")
    assert card.severity == "idle"
    assert card.findings_count == 0
    assert card.empty_state is not None
    assert "7 days" in card.empty_state


def test_openai_only_projection_when_no_anthropic(
    clean_budgets: None,
) -> None:
    """A tenant with ONLY OpenAI spend (≥7 days) still projects — the
    cross-vendor card doesn't go idle just because Anthropic is empty.

    This is the honest cross-vendor behavior: an OpenAI-only customer
    sees their forecast."""
    from vargate_telemetry.insights.cards import cost_forecasting

    tenant = _tid("forecast_oai_only")
    _provision_tenant(tenant)
    _seed_openai_rising_days(tenant, 9)

    forecasts = cost_forecasting.vendor_forecasts(tenant)
    assert {vf.vendor for vf in forecasts} == {"OpenAI"}

    card = cost_forecasting.build_card(tenant, "7d")
    # No budget → idle-but-with-projection sentence + CTA.
    assert card.findings_count == 0
    assert card.empty_state is not None
    assert "on current pace" in card.empty_state.lower()
    assert card.cta is not None
