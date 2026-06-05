# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the forecast-detail endpoint (TM7) — ``GET /insights/forecast``.

This endpoint backs the Cost-forecasting drill-in page: it serialises
``insights.spend_data.project_period_end`` (the trailing daily-spend
series + the month-end projection) plus the active monthly tenant
budget caps, as plain JSON numbers the frontend chart consumes.

Coverage:
  - 401 when unauthenticated.
  - 400 ``no_tenant_bound`` when the JWT carries no tenant.
  - Happy path: ≥8 rising days + a monthly tenant budget → the response
    shape (period bounds as ISO dates, numeric current/projected spend,
    a daily_series of {date, usd}, the budget echoed in ``budgets``).
  - Empty tenant: no usage, no budgets → an all-zero, empty-series
    payload (not a 500), so the page renders its not-enough-data state.

Seeds the same synthetic ``telemetry_records`` + budget rows the
forecasting-card test uses; budgets are inserted under ``session_scope``
so the RLS WITH CHECK is satisfied.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

os.environ.setdefault(
    "JWT_SIGNING_KEY",
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b",
)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ───────────────────────────────────────────────────────────────────────────
# Fixtures + helpers (copied from the sibling insights / budgets suites)
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


@pytest.fixture
def clean() -> Iterator[None]:
    from vargate_telemetry.db import engine

    def _truncate() -> None:
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


def _bearer(tenant_id: str | None) -> dict[str, str]:
    from vargate_telemetry.auth.jwt import issue_session_jwt

    token = issue_session_jwt(
        user_id=str(uuid.uuid4()),
        email="probe@example.com",
        sso_provider="google",
        tenant_id=tenant_id,
    )
    return {"Authorization": f"Bearer {token}"}


_SONNET = "claude-sonnet-4-5-20250929"


def _seed_usage_record(
    tenant_id: str,
    *,
    occurred_at: datetime,
    input_tokens: int = 1_000_000,
    output_tokens: int = 0,
) -> None:
    from vargate_telemetry.db import engine

    md = {
        "starting_at": occurred_at.isoformat(),
        "ending_at": occurred_at.isoformat(),
        "results": [
            {
                "model": _SONNET,
                "workspace_id": None,
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


def _seed_rising_days(tenant_id: str, num_days: int) -> None:
    """``num_days`` distinct recent UTC days of rising spend (positive slope)."""
    for k in range(num_days):
        _seed_usage_record(
            tenant_id,
            occurred_at=datetime.now(tz=timezone.utc) - timedelta(days=k),
            input_tokens=1_000_000 * (num_days - k),
            output_tokens=0,
        )


def _insert_monthly_tenant_budget(
    tenant_id: str, *, threshold_usd: str, name: str
) -> None:
    """Insert a live monthly tenant-scope budget under session_scope (RLS)."""
    from vargate_telemetry.db import session_scope

    with session_scope(tenant_id) as s:
        s.execute(
            sql_text(
                """
                INSERT INTO budgets (
                    tenant_id, name, scope_kind, scope_value,
                    period, threshold_usd, alert_recipients
                ) VALUES (
                    :t, :name, 'tenant', NULL,
                    'monthly', :threshold, CAST('{}' AS jsonb)
                )
                """
            ),
            {"t": tenant_id, "name": name, "threshold": threshold_usd},
        )


# ───────────────────────────────────────────────────────────────────────────
# (a) Auth guards
# ───────────────────────────────────────────────────────────────────────────


def test_forecast_requires_auth(clean: None, client: TestClient) -> None:
    assert client.get("/insights/forecast").status_code == 401


def test_forecast_no_tenant_bound_returns_400(
    clean: None, client: TestClient
) -> None:
    r = client.get("/insights/forecast", headers=_bearer(None))
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "no_tenant_bound"


# ───────────────────────────────────────────────────────────────────────────
# (b) Happy path — projection + budget cap, JSON-number serialisation
# ───────────────────────────────────────────────────────────────────────────


def test_forecast_happy_path(clean: None, client: TestClient) -> None:
    tenant = _tid("forecast_ok")
    _provision_tenant(tenant)
    _seed_rising_days(tenant, 9)  # ≥8 distinct rising days
    _insert_monthly_tenant_budget(
        tenant, threshold_usd="500.00", name="Monthly cap"
    )

    r = client.get("/insights/forecast", headers=_bearer(tenant))
    assert r.status_code == 200, r.text
    body = r.json()

    # Shape. ``vendors`` (TM8 Phase D) is the per-vendor breakdown — an
    # additive field; the rest is the original TM7 shape.
    assert set(body) == {
        "period_start",
        "period_end",
        "current_spend",
        "projected_end",
        "days_remaining",
        "days_of_data",
        "daily_series",
        "budgets",
        "vendors",
    }
    # Period bounds are ISO dates; the period starts on the 1st.
    assert _ISO_DATE.match(body["period_start"])
    assert _ISO_DATE.match(body["period_end"])
    assert body["period_start"].endswith("-01")

    # Numeric money (plain JSON numbers, not strings).
    assert isinstance(body["current_spend"], (int, float))
    assert isinstance(body["projected_end"], (int, float))
    assert body["current_spend"] > 0
    assert body["projected_end"] >= body["current_spend"]
    assert isinstance(body["days_remaining"], int)
    assert body["days_of_data"] >= 8

    # The daily series is a list of {date, usd} with ISO dates + numbers.
    series = body["daily_series"]
    assert isinstance(series, list) and len(series) >= 8
    pt = series[0]
    assert set(pt) == {"date", "usd"}
    assert _ISO_DATE.match(pt["date"])
    assert isinstance(pt["usd"], (int, float))

    # The seeded budget is echoed back for the cap overlay.
    assert body["budgets"] == [{"name": "Monthly cap", "threshold_usd": 500.0}]

    # Per-vendor breakdown (TM8 Phase D). This fixture is Anthropic-only,
    # so a single "Anthropic" entry, basis "estimated", whose figures
    # equal the cross-vendor totals (no other vendor to add).
    vendors = body["vendors"]
    assert isinstance(vendors, list) and len(vendors) == 1
    av = vendors[0]
    assert set(av) == {
        "vendor",
        "basis",
        "current_spend",
        "projected_end",
        "daily_series",
    }
    assert av["vendor"] == "Anthropic"
    assert av["basis"] == "estimated"
    assert av["projected_end"] == body["projected_end"]
    assert av["current_spend"] == body["current_spend"]
    # Its per-vendor series matches the combined series (only vendor).
    assert av["daily_series"] == series


# ───────────────────────────────────────────────────────────────────────────
# (c) Empty tenant — all-zero, empty-series payload (never a 500)
# ───────────────────────────────────────────────────────────────────────────


def test_forecast_empty_tenant(clean: None, client: TestClient) -> None:
    tenant = _tid("forecast_empty")
    _provision_tenant(tenant)

    r = client.get("/insights/forecast", headers=_bearer(tenant))
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["daily_series"] == []
    assert body["budgets"] == []
    assert body["days_of_data"] == 0
    assert body["current_spend"] == 0
    assert body["projected_end"] == 0
    # Period bounds are still populated so the page can render its axis.
    assert _ISO_DATE.match(body["period_start"])
    assert _ISO_DATE.match(body["period_end"])
