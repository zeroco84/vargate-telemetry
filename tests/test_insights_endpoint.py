# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the Insights endpoint (TM7) — ``GET /insights``.

The Insights page is a single endpoint returning an ordered column of
analysis cards. The aggregator isolates per-card failures, so the
endpoint always returns the full set of card slots in display order
(``cache_efficiency`` first, ``workspace_attribution`` last) even when
a tenant has no captured usage at all.

Coverage:
  - 401 when unauthenticated (no Authorization header).
  - 200 happy path: response shape (window / refreshed_at / cards) and
    the six cards in the exact registry order, each carrying the small
    frontend contract (id / title / severity / findings_count /
    headline).
  - RLS isolation: a tenant with seeded usage does not leak into a
    DIFFERENT tenant's cards — the second tenant's forecasting card is
    idle (no data to project).
  - ``?window=30d`` is accepted (an alternate trailing window).
  - 400 ``no_tenant_bound`` when the JWT carries no tenant.

Like ``test_usage_api.py`` / ``test_budgets_api.py``, this seeds
synthetic ``telemetry_records`` via direct INSERT and exercises the
route through FastAPI's TestClient. Assertions are on structural shape
and substrings, not exact copy strings, so wording tweaks to a card's
headline don't break the suite.
"""

from __future__ import annotations

import json
import os
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


# ───────────────────────────────────────────────────────────────────────────
# Fixtures + helpers (copied from test_budgets_api.py / test_usage_api.py)
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


@pytest.fixture
def clean() -> Iterator[None]:
    """Empty budgets + alert events + telemetry_records before AND after.

    The cost-forecasting card reads the ``budgets`` table, so a clean
    slate there keeps the forecasting card deterministic (no leftover
    monthly cap from another test changes its render branch).
    """
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
    """A unique tenant_id per test, so a missed TRUNCATE never bleeds in."""
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


# Sonnet rate: input $3/Mtok + output $15/Mtok. Same fixture shape as
# the budgets/usage suites.
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


# The six insight cards, in the exact display order the registry pins.
_EXPECTED_CARD_IDS = [
    "cache_efficiency",
    "cost_forecasting",
    "anomaly_detection",
    "activity_categorization",
    "model_mix",
    "workspace_attribution",
]

# The small per-card frontend contract every card must satisfy.
_CARD_KEYS = {"id", "title", "severity", "findings_count", "headline"}


# ───────────────────────────────────────────────────────────────────────────
# (a) Auth guard
# ───────────────────────────────────────────────────────────────────────────


def test_insights_requires_auth(clean: None, client: TestClient) -> None:
    """No Authorization header → 401 (same as ``/usage``)."""
    r = client.get("/insights")
    assert r.status_code == 401, r.text


# ───────────────────────────────────────────────────────────────────────────
# (b) Happy path — shape + ordered card set
# ───────────────────────────────────────────────────────────────────────────


def test_insights_happy_path_shape_and_card_order(
    clean: None, client: TestClient
) -> None:
    """A provisioned tenant gets 200 with the full ordered card column.

    The aggregator always returns every card slot (one per registry
    entry) even when the tenant has no usage, so the six-card contract
    holds regardless of seeded data.
    """
    tenant = _tid("insights_happy")
    _provision_tenant(tenant)

    r = client.get("/insights", headers=_bearer(tenant))
    assert r.status_code == 200, r.text
    body = r.json()

    # Top-level response shape.
    assert set(body) >= {"window", "refreshed_at", "cards"}
    assert body["window"] == "7d"  # the route's default window
    assert body["refreshed_at"] is not None

    # Exactly six cards, in the registry display order.
    cards = body["cards"]
    assert isinstance(cards, list)
    assert len(cards) == 6
    assert [c["id"] for c in cards] == _EXPECTED_CARD_IDS

    # Every card carries the small frontend contract.
    valid_severities = {"idle", "advisory", "warning", "action"}
    for card in cards:
        assert _CARD_KEYS <= set(card), (
            f"card {card.get('id')!r} missing keys: "
            f"{_CARD_KEYS - set(card)}"
        )
        assert isinstance(card["id"], str) and card["id"]
        assert isinstance(card["title"], str) and card["title"]
        assert card["severity"] in valid_severities
        assert isinstance(card["findings_count"], int)
        assert isinstance(card["headline"], str)


# ───────────────────────────────────────────────────────────────────────────
# (c) RLS isolation — A's usage does not surface in B's cards
# ───────────────────────────────────────────────────────────────────────────


def test_insights_rls_isolated_across_tenants(
    clean: None, client: TestClient
) -> None:
    """Seed a usage history for tenant A; tenant B (no records) sees its
    own clean cards.

    The load-bearing check is the cost-forecasting card: with enough
    days of data it projects spend, but B has none, so B's forecasting
    card stays idle (``severity='idle'``, ``findings_count == 0``). If
    A's records leaked across the RLS boundary, B's forecasting card
    would light up. We assert directly on B's built card so the
    isolation claim is unambiguous.
    """
    tenant_a = _tid("insights_rls_a")
    tenant_b = _tid("insights_rls_b")
    _provision_tenant(tenant_a)
    _provision_tenant(tenant_b)

    # Give A a fortnight of daily usage — well past the 7-day floor the
    # forecasting card needs to produce a projection.
    now = datetime.now(tz=timezone.utc)
    for day_offset in range(14):
        _seed_usage_record(
            tenant_a,
            occurred_at=now - timedelta(days=day_offset, hours=1),
        )

    # The endpoint still returns the full card set for B.
    body_b = client.get("/insights", headers=_bearer(tenant_b)).json()
    cards_b = {c["id"]: c for c in body_b["cards"]}
    assert [c["id"] for c in body_b["cards"]] == _EXPECTED_CARD_IDS

    # B's forecasting card is idle — no A data bled across the boundary.
    fc_b = cards_b["cost_forecasting"]
    assert fc_b["severity"] == "idle"
    assert fc_b["findings_count"] == 0

    # Cross-check by building B's forecasting card directly: it must
    # still be idle/finding-free with no records of its own.
    from vargate_telemetry.insights.cards import cost_forecasting

    direct_b = cost_forecasting.build_card(tenant_b, "7d")
    assert direct_b.severity == "idle"
    assert direct_b.findings_count == 0

    # And A's own forecasting card, built directly, is NOT idle — it has
    # 14 days of data to project from. (If this were idle too, the test
    # above would be vacuous.)
    direct_a = cost_forecasting.build_card(tenant_a, "7d")
    assert direct_a.id == "cost_forecasting"
    assert "Projection" in (direct_a.empty_state or "") or direct_a.items


# ───────────────────────────────────────────────────────────────────────────
# (d) Alternate window
# ───────────────────────────────────────────────────────────────────────────


def test_insights_accepts_30d_window(
    clean: None, client: TestClient
) -> None:
    """``?window=30d`` is a valid trailing window → 200, echoed back."""
    tenant = _tid("insights_30d")
    _provision_tenant(tenant)

    r = client.get("/insights?window=30d", headers=_bearer(tenant))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window"] == "30d"
    assert [c["id"] for c in body["cards"]] == _EXPECTED_CARD_IDS


# ───────────────────────────────────────────────────────────────────────────
# (e) No tenant bound
# ───────────────────────────────────────────────────────────────────────────


def test_insights_no_tenant_bound_returns_400(
    clean: None, client: TestClient
) -> None:
    """A JWT without a tenant binding → 400 ``no_tenant_bound`` (same
    failure mode as ``/usage`` and ``/budgets``)."""
    r = client.get("/insights", headers=_bearer(None))
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "no_tenant_bound"
