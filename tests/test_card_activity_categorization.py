# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the activity-categorization Insights card (TM7).

This card is a finding-free placeholder until the TM5 classification
engine lands. It reads no DB and issues no spend query, so the tests
call :func:`build_card` directly and assert on the idle-card shape:
severity ``idle``, ``findings_count`` 0, no ``cta``, an ``empty_state``
that describes the coming topic-area classification (and names TM5),
and a ``headline`` that reads "Coming next release".

The fixtures + helpers (client, clean, _bearer, _provision_tenant,
_seed_usage_record) mirror ``test_budgets_api.py`` / ``test_usage_api.py``
verbatim so this file stays consistent with the rest of the suite,
even though this card needs no seeding.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

os.environ.setdefault(
    "JWT_SIGNING_KEY",
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b",
)


# ───────────────────────────────────────────────────────────────────────────
# Fixtures + helpers (mirrored from test_budgets_api / test_usage_api)
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


@pytest.fixture
def clean() -> Iterator[None]:
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


# ───────────────────────────────────────────────────────────────────────────
# build_card — the placeholder idle card (no seeding required)
# ───────────────────────────────────────────────────────────────────────────


def _unique_tenant() -> str:
    return "tnt_us_activitycat_" + uuid.uuid4().hex[:8]


def test_activity_categorization_is_idle() -> None:
    """No data exists for this card yet (the TM5 classifier hasn't
    shipped), so it returns an idle, finding-free card regardless of
    tenant. No seeding required — ``build_card`` reads no DB."""
    from vargate_telemetry.insights.cards.activity_categorization import (
        build_card,
    )

    card = build_card(_unique_tenant(), "7d")
    assert card.severity == "idle"
    assert card.findings_count == 0
    assert card.cta is None


def test_activity_categorization_empty_state_names_topics_and_tm5() -> None:
    """The empty-state copy tells the operator what this card will
    become: it mentions "topic areas" and names the TM5 release that
    ships the classifier. Substring checks so wording tweaks elsewhere
    in the sentence don't break the test."""
    from vargate_telemetry.insights.cards.activity_categorization import (
        build_card,
    )

    card = build_card(_unique_tenant(), "7d")
    assert card.empty_state is not None
    assert "topic areas" in card.empty_state
    assert "TM5" in card.empty_state


def test_activity_categorization_headline_is_coming_next_release() -> None:
    """``findings_count`` is 0 so the UI ignores the headline, but the
    contract still sets it to "Coming next release" — assert the
    substring so the placeholder framing is pinned."""
    from vargate_telemetry.insights.cards.activity_categorization import (
        build_card,
    )

    card = build_card(_unique_tenant(), "7d")
    assert "Coming next release" in card.headline
