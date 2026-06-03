# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the forecast budget-alert task (TM7).

Companion to ``test_evaluate_budgets.py`` (the current-threshold
evaluator). These exercise ``evaluate_forecasts_for_tenant``, which
fires when month-to-date spend is *projected* to reach a threshold by
month-end on the current pace — the early-warning sibling of the
already-crossed alert.

Covered:
  (a) a rising-usage tenant with a small monthly cap fires a
      ``forecast_threshold`` alert event AND calls ``send_budget_alert``
      with a ``forecast_threshold`` context.
  (b) a second tick within the same period is a silent no-op — no new
      forecast row (the widened 4-column dedup), ``send_budget_alert``
      not called again.
  (c) kind-independence: a ``current_threshold`` row for the same
      ``(budget_id, period_start, threshold_crossed)`` does NOT block a
      ``forecast_threshold`` row for the same triple — distinct ``kind``
      means distinct dedup lane (migration 0024).

The ``send_budget_alert`` symbol is patched as it is imported into the
task module (``vargate_telemetry.tasks.evaluate_forecasts``) so no real
notification is dispatched; the patch records every
``BudgetAlertContext`` it was called with.
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

from vargate_telemetry.tasks import evaluate_forecasts as ef_mod
from vargate_telemetry.tasks.evaluate_forecasts import (
    evaluate_forecasts_for_tenant,
)

os.environ.setdefault(
    "JWT_SIGNING_KEY",
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b",
)


# ───────────────────────────────────────────────────────────────────────────
# Fixtures + helpers
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_state() -> Iterator[None]:
    """Empty budgets + alert events + telemetry before AND after."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE budget_alert_events, budgets "
                "RESTART IDENTITY CASCADE"
            )
        )
        conn.execute(
            sql_text(
                "TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE"
            )
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
            sql_text(
                "TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE"
            )
        )


class _AlertRecorder:
    """Stand-in for ``send_budget_alert`` — records the ctx, sends nothing.

    The real function returns a per-channel summary dict (empty when no
    channel has recipients). We mirror that "alert recorded, nobody
    notified" return so the task's best-effort dispatch logging is happy,
    while capturing the ``BudgetAlertContext`` for assertions.
    """

    def __init__(self) -> None:
        self.contexts: list = []

    def __call__(self, recipients=None, ctx=None) -> dict:  # noqa: ANN001
        self.contexts.append(ctx)
        return {}


@pytest.fixture
def recorded_alert(monkeypatch: pytest.MonkeyPatch) -> _AlertRecorder:
    """Patch ``send_budget_alert`` as bound in the task module.

    The task does ``from vargate_telemetry.notify import send_budget_alert``
    at import time, so the live reference is
    ``ef_mod.send_budget_alert`` — that's the name we replace.
    """
    recorder = _AlertRecorder()
    monkeypatch.setattr(ef_mod, "send_budget_alert", recorder)
    return recorder


def _provision_tenant_and_user(tenant_id: str) -> str:
    """Provision a real tenant + user. Returns the user UUID."""
    user_uuid = str(uuid.uuid4())
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO tenants (tenant_id, region, active, billing_status)
                VALUES (:t, 'us', TRUE, 'trial')
                ON CONFLICT (tenant_id) DO NOTHING
                """
            ),
            {"t": tenant_id},
        )
        conn.execute(
            sql_text(
                """
                INSERT INTO users
                    (id, email, sso_provider, sso_subject_id, tenant_id)
                VALUES (:id, :email, 'google', :sub, :t)
                """
            ),
            {
                "id": user_uuid,
                "email": f"u-{user_uuid[:8]}@example.com",
                "sub": f"sub-{user_uuid}",
                "t": tenant_id,
            },
        )
    return user_uuid


# Sonnet rate: $3/Mtok in + $15/Mtok out. 1M in + 200k out = $6.00/day.
_SONNET = "claude-sonnet-4-5-20250929"


def _seed_usage_record(
    tenant_id: str,
    *,
    occurred_at: datetime,
    input_tokens: int = 1_000_000,
    output_tokens: int = 200_000,
    model: str | None = _SONNET,
    workspace_id: str | None = None,
    api_key_id: str | None = None,
) -> None:
    """Insert one ``usage``/``admin`` record at ``occurred_at``.

    Mirrors ``test_budgets_api._seed_usage_record`` /
    ``test_evaluate_budgets._seed_usage``: a ``metadata.results`` array
    with a single per-model breakdown, ``chain_seq`` = COALESCE(MAX+1),
    and placeholder content/prev/self hashes via ``decode(..., 'hex')``.
    """
    from vargate_telemetry.db import engine

    md = {
        "starting_at": occurred_at.isoformat(),
        "ending_at": occurred_at.isoformat(),
        "results": [
            {
                "model": model,
                "workspace_id": workspace_id,
                "api_key_id": api_key_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }
        ],
    }
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
                "eid": f"usage:{uuid.uuid4()}",
                "occurred_at": occurred_at,
                "metadata": json.dumps(md),
                "zero32": "00" * 32,
                "one32": "11" * 32,
            },
        )


def _seed_rising_usage(tenant_id: str, *, days: int = 12) -> None:
    """Seed ``days`` consecutive recent days of rising daily spend.

    One record per day from ``today - (days - 1)`` through ``today``,
    with the per-day token count growing day over day so the
    trailing-14-day least-squares slope is strongly positive. ``days``
    >= 8 guarantees ``days_of_data >= 7`` (the forecast guardrail),
    and several of the most-recent days land inside the current UTC
    month so the month-to-date ``current_spend`` is non-trivial.
    """
    now = datetime.now(timezone.utc)
    for back in range(days):
        # Oldest day is the smallest; today is the largest → rising.
        step = days - back  # 1..days, newest largest
        occurred = now - timedelta(days=back, hours=1)
        _seed_usage_record(
            tenant_id,
            occurred_at=occurred,
            input_tokens=1_000_000 * step,
            output_tokens=200_000 * step,
        )


def _create_budget(
    tenant_id: str,
    *,
    user_uuid: str,
    name: str = "Forecast monthly",
    threshold_usd: Decimal = Decimal("2.00"),
    recipients: list[str] | None = None,
) -> str:
    """INSERT a tenant-wide monthly budgets row; return its id.

    Only tenant-wide monthly budgets are forecast (the task scopes its
    SELECT to ``scope_kind='tenant' AND period='monthly'``), so the
    defaults match what the forecaster actually reads.
    """
    from vargate_telemetry.db import session_scope

    with session_scope(tenant_id) as s:
        row = s.execute(
            sql_text(
                """
                INSERT INTO budgets (
                    tenant_id, name, scope_kind, scope_value,
                    period, threshold_usd, alert_recipients,
                    created_by_user_id
                ) VALUES (
                    :t, :name, 'tenant', NULL,
                    'monthly', :threshold, CAST(:recipients AS jsonb),
                    :user_uuid
                )
                RETURNING id::text
                """
            ),
            {
                "t": tenant_id,
                "name": name,
                "threshold": threshold_usd,
                "recipients": json.dumps(
                    {
                        "email": list(recipients or []),
                        "slack_webhook": [],
                        "pagerduty_key": [],
                    }
                ),
                "user_uuid": user_uuid,
            },
        ).one()
    return row.id


def _current_period_start() -> "object":
    """First day of the current UTC month — what the task computes.

    Returned as a ``date`` so it can be bound straight into a SQL
    parameter for the manual-INSERT in the independence test.
    """
    return datetime.now(timezone.utc).date().replace(day=1)


def _count_forecast_alerts(tenant_id: str, budget_id: str) -> int:
    from vargate_telemetry.db import session_scope

    with session_scope(tenant_id) as s:
        row = s.execute(
            sql_text(
                "SELECT COUNT(*) AS n FROM budget_alert_events "
                "WHERE budget_id = :b AND kind = 'forecast_threshold'"
            ),
            {"b": budget_id},
        ).one()
    return int(row.n)


# ───────────────────────────────────────────────────────────────────────────
# (a) rising usage → forecast_threshold alert fires
# ───────────────────────────────────────────────────────────────────────────


def test_rising_usage_fires_forecast_threshold_alert(
    clean_state: None, recorded_alert: _AlertRecorder
) -> None:
    """8+ days of rising spend + a small monthly cap → the projection
    crosses >=70% and a ``forecast_threshold`` alert event lands, with a
    matching ``forecast_threshold`` context handed to send_budget_alert."""
    tenant = "tnt_us_forecast_fire_" + uuid.uuid4().hex[:8]
    user = _provision_tenant_and_user(tenant)
    _seed_rising_usage(tenant, days=12)
    budget_id = _create_budget(
        tenant,
        user_uuid=user,
        threshold_usd=Decimal("2.00"),
        recipients=["rick@vargate.ai"],
    )

    result = evaluate_forecasts_for_tenant(tenant)

    # The monthly tenant budget was checked and at least one threshold
    # projection fired.
    assert result["budgets_checked"] == 1
    assert len(result["thresholds_fired"]) >= 1
    # The lowest threshold (0.70) is always among those fired when the
    # projection crosses anything.
    assert any(t.endswith(":0.70") for t in result["thresholds_fired"])

    # A forecast_threshold alert row landed for this budget.
    assert _count_forecast_alerts(tenant, budget_id) >= 1

    # send_budget_alert was called, and every recorded context is a
    # forecast (not a current-threshold) alert.
    assert recorded_alert.contexts, "send_budget_alert was never called"
    ctx = recorded_alert.contexts[0]
    assert ctx.kind == "forecast_threshold"
    # The forecast context carries a projected breach date (the task
    # always sets one for a forecast alert).
    assert ctx.projected_breach_date is not None
    assert ctx.scope_kind == "tenant"


# ───────────────────────────────────────────────────────────────────────────
# (b) second tick within the same period → dedup, no new row / no resend
# ───────────────────────────────────────────────────────────────────────────


def test_second_tick_does_not_refire_forecast(
    clean_state: None, recorded_alert: _AlertRecorder
) -> None:
    """The forecast beat ticks repeatedly. After the first tick fires,
    a second tick within the same period must NOT insert a new
    forecast row and must NOT call send_budget_alert again — the widened
    4-column dedup ``(budget_id, period_start, threshold_crossed, kind)``."""
    tenant = "tnt_us_forecast_dedup_" + uuid.uuid4().hex[:8]
    user = _provision_tenant_and_user(tenant)
    _seed_rising_usage(tenant, days=12)
    budget_id = _create_budget(
        tenant,
        user_uuid=user,
        threshold_usd=Decimal("2.00"),
        recipients=["rick@vargate.ai"],
    )

    # First tick: at least one forecast alert fires.
    first = evaluate_forecasts_for_tenant(tenant)
    assert len(first["thresholds_fired"]) >= 1
    rows_after_first = _count_forecast_alerts(tenant, budget_id)
    assert rows_after_first >= 1
    calls_after_first = len(recorded_alert.contexts)
    assert calls_after_first >= 1

    # Second tick, same data, same period — nothing new.
    second = evaluate_forecasts_for_tenant(tenant)
    assert second["thresholds_fired"] == []
    # Row count unchanged (ON CONFLICT DO NOTHING swallowed the re-insert).
    assert _count_forecast_alerts(tenant, budget_id) == rows_after_first
    # send_budget_alert not called again.
    assert len(recorded_alert.contexts) == calls_after_first


# ───────────────────────────────────────────────────────────────────────────
# (c) kind-independence: a current_threshold row does not block a
#     forecast_threshold row for the same (budget, period, threshold)
# ───────────────────────────────────────────────────────────────────────────


def test_forecast_row_independent_of_current_threshold_row(
    clean_state: None, recorded_alert: _AlertRecorder
) -> None:
    """A current_threshold alert and a forecast_threshold alert for the
    SAME (budget_id, period_start, threshold_crossed) are distinct dedup
    lanes (migration 0024 widened the key with ``kind``). Pre-seed a
    current_threshold row for the 0.70 threshold, then run the
    forecaster: the forecast_threshold row for the same triple still
    inserts."""
    tenant = "tnt_us_forecast_indep_" + uuid.uuid4().hex[:8]
    user = _provision_tenant_and_user(tenant)
    _seed_rising_usage(tenant, days=12)
    budget_id = _create_budget(
        tenant,
        user_uuid=user,
        threshold_usd=Decimal("2.00"),
        recipients=["rick@vargate.ai"],
    )

    period_start = _current_period_start()

    # Manually INSERT a CURRENT-threshold row occupying the
    # (budget_id, period_start, 0.70) triple in the current_threshold
    # lane. If the dedup key didn't include `kind`, this would block the
    # forecaster's INSERT for the same triple.
    from vargate_telemetry.db import session_scope

    with session_scope(tenant) as s:
        s.execute(
            sql_text(
                """
                INSERT INTO budget_alert_events (
                    budget_id, tenant_id, period_start,
                    threshold_crossed, current_spend_usd, kind
                ) VALUES (
                    :b, current_setting('app.tenant_id'), :ps,
                    0.70, 1.50, 'current_threshold'
                )
                """
            ),
            {"b": budget_id, "ps": period_start},
        )

    # Now run the forecaster.
    result = evaluate_forecasts_for_tenant(tenant)

    # It fired the 0.70 forecast threshold despite the current_threshold
    # row already occupying the same triple.
    assert any(t.endswith(":0.70") for t in result["thresholds_fired"])

    # Both lanes are populated for the SAME (budget, period, threshold):
    # one current_threshold + one forecast_threshold.
    with session_scope(tenant) as s:
        rows = s.execute(
            sql_text(
                """
                SELECT kind
                FROM budget_alert_events
                WHERE budget_id = :b
                  AND period_start = :ps
                  AND threshold_crossed = 0.70
                ORDER BY kind
                """
            ),
            {"b": budget_id, "ps": period_start},
        ).all()
    kinds = {r.kind for r in rows}
    assert "current_threshold" in kinds
    assert "forecast_threshold" in kinds
    # Exactly the two distinct-kind rows for that triple (no duplicates).
    assert len(rows) == 2
