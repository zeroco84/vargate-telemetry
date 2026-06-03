# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the TM7 anomaly-detection insight card.

The card is a deliberate TM7 placeholder: the detection engine
(off-hours bursts, per-actor volume spikes, model-mix shifts vs a
rolling 14-day baseline) lands in TM5. Until then ``build_card``
ignores both ``tenant_id`` and ``window`` and always returns the same
idle, finding-free card — it never issues a DB query, so no seeding is
required here.

Coverage:
  - ``build_card`` returns severity ``idle``, ``findings_count`` 0,
    ``cta`` None.
  - ``empty_state`` mentions what's being "watching for" + the TM5
    landing.
  - ``headline`` reports "No anomalies".
  - the window argument is ignored (every window resolves to the same
    idle card).
  - the card surfaces through ``GET /insights`` for a tenant with no
    data.

Assertions are on substrings / structural shape (not full copy
strings) so wording tweaks to the placeholder text don't break tests.
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

os.environ.setdefault(
    "JWT_SIGNING_KEY",
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b",
)


# ───────────────────────────────────────────────────────────────────────────
# Fixtures + helpers
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


@pytest.fixture
def clean_records() -> Iterator[None]:
    """Empty telemetry_records before AND after each test.

    The card never queries the DB, but the endpoint-level test runs the
    aggregator under a real tenant; truncating keeps the run hermetic.
    """
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


def _tenant(name: str) -> str:
    """A unique tenant id per test so runs never collide."""
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


# ───────────────────────────────────────────────────────────────────────────
# build_card — the idle placeholder (no seeding)
# ───────────────────────────────────────────────────────────────────────────


def test_build_card_is_idle_and_finding_free() -> None:
    """The TM7 placeholder card: idle severity, zero findings, no CTA."""
    from vargate_telemetry.insights.cards.anomaly_detection import (
        CARD_ID,
        build_card,
    )

    card = build_card(_tenant("anomaly_idle"), "7d")

    assert card.id == CARD_ID
    assert card.severity == "idle"
    assert card.findings_count == 0
    assert card.cta is None
    assert card.items == []


def test_build_card_empty_state_mentions_watching_and_tm5() -> None:
    """The empty-state copy tells the operator what's coming + when.

    ``findings_count == 0`` means the UI renders ``empty_state`` in
    place of the headline, so this is the user-visible string.
    """
    from vargate_telemetry.insights.cards.anomaly_detection import build_card

    card = build_card(_tenant("anomaly_empty"), "7d")

    assert card.empty_state is not None
    assert "watching for" in card.empty_state
    assert "TM5" in card.empty_state


def test_build_card_headline_reports_no_anomalies() -> None:
    from vargate_telemetry.insights.cards.anomaly_detection import build_card

    card = build_card(_tenant("anomaly_headline"), "7d")

    assert "No anomalies" in card.headline


def test_build_card_ignores_window() -> None:
    """Every window resolves to the same idle card (nothing to compute
    yet). A different window must not change the shape."""
    from vargate_telemetry.insights.cards.anomaly_detection import build_card

    tenant = _tenant("anomaly_window")
    seven = build_card(tenant, "7d")
    thirty = build_card(tenant, "30d")

    assert thirty.severity == "idle"
    assert thirty.findings_count == 0
    assert thirty.empty_state == seven.empty_state
    assert thirty.headline == seven.headline


# ───────────────────────────────────────────────────────────────────────────
# GET /insights — the card surfaces through the aggregator
# ───────────────────────────────────────────────────────────────────────────


def test_anomaly_card_present_in_insights_response(
    clean_records: None, client: TestClient
) -> None:
    """A tenant with no data still gets the anomaly card on the page,
    rendered as an idle placeholder."""
    from vargate_telemetry.insights.cards.anomaly_detection import CARD_ID

    tenant = _tenant("anomaly_endpoint")
    _provision_tenant(tenant)

    r = client.get("/insights?window=7d", headers=_bearer(tenant))
    assert r.status_code == 200, r.text
    body = r.json()

    cards_by_id = {c["id"]: c for c in body["cards"]}
    assert CARD_ID in cards_by_id
    card = cards_by_id[CARD_ID]
    assert card["severity"] == "idle"
    assert card["findings_count"] == 0
    assert card["cta"] is None
    assert "watching for" in card["empty_state"]
    assert "TM5" in card["empty_state"]
    assert "No anomalies" in card["headline"]
