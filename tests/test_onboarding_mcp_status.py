# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""TM2 Phase D1 — GET /onboarding/mcp-status tests.

The SPA polls this endpoint after the user kicks off MCP setup,
watching for the first mcp row to land in telemetry_records.

Cases:

  - No session → 401 (the existing auth middleware does this; we
    verify the surface still requires auth).
  - Signed-in but pre-tenant (mid-onboarding) → not-configured shape.
  - Signed-in + tenant + no MCP rows yet → not-configured shape.
  - Signed-in + tenant + N MCP rows → configured=true, count=N,
    first_event_at = earliest occurred_at.
  - Rows >90 days old are excluded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


@pytest.fixture
def clean_records() -> Iterator[None]:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE"
            )
        )
    yield
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE"
            )
        )


def _bearer(
    *,
    user_id: str = "user-mcp-status",
    email: str = "mcp-status@example.com",
    tenant_id: str | None = "tnt_us_mcp_status",
) -> dict[str, str]:
    from vargate_telemetry.auth.jwt import issue_session_jwt

    token = issue_session_jwt(
        user_id=user_id,
        email=email,
        sso_provider="google",
        tenant_id=tenant_id,
    )
    return {"Authorization": f"Bearer {token}"}


def _persist_mcp_row(
    *,
    tenant_id: str,
    days_old: int = 0,
) -> None:
    """Persist one mcp row via the real Celery task body (no broker)."""
    from mcp_server.tasks.persist_event import persist_event

    occurred = datetime.now(timezone.utc) - timedelta(days=days_old)
    persist_event.run(
        event_id=str(uuid4()),
        tenant_id=tenant_id,
        user_id="user-mcp-status-row",
        user_email="row@example.com",
        kind="chat",
        model="claude-opus-4-7",
        summary="row for /mcp-status test",
        input_tokens_estimate=100,
        output_tokens_estimate=50,
        tool_calls_count=1,
        client_received_at=occurred.isoformat(),
    )


# ───────────────────────────────────────────────────────────────────────────
# Auth surface
# ───────────────────────────────────────────────────────────────────────────


def test_no_session_returns_401(client: TestClient) -> None:
    response = client.get("/onboarding/mcp-status")
    assert response.status_code == 401


# ───────────────────────────────────────────────────────────────────────────
# Empty / pre-tenant
# ───────────────────────────────────────────────────────────────────────────


def test_pre_tenant_user_gets_not_configured_shape(
    client: TestClient,
    clean_records: None,
) -> None:
    """A mid-onboarding user (no tenant_id) can't have MCP traffic."""
    response = client.get(
        "/onboarding/mcp-status",
        headers=_bearer(tenant_id=None),
    )
    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "first_event_at": None,
        "events_count": 0,
    }


def test_tenant_with_no_mcp_rows_returns_not_configured(
    client: TestClient,
    clean_records: None,
) -> None:
    """Tenant exists but no MCP rows yet → configured=False."""
    response = client.get(
        "/onboarding/mcp-status",
        headers=_bearer(tenant_id="tnt_us_mcp_status_zero"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["first_event_at"] is None
    assert body["events_count"] == 0


# ───────────────────────────────────────────────────────────────────────────
# Populated
# ───────────────────────────────────────────────────────────────────────────


def test_first_event_flips_to_configured(
    client: TestClient,
    clean_records: None,
) -> None:
    """One mcp row → configured=True, count=1, first_event_at set."""
    _persist_mcp_row(tenant_id="tnt_us_mcp_status_one")
    response = client.get(
        "/onboarding/mcp-status",
        headers=_bearer(tenant_id="tnt_us_mcp_status_one"),
    )
    body = response.json()
    assert body["configured"] is True
    assert body["events_count"] == 1
    assert body["first_event_at"] is not None


def test_three_events_aggregate_correctly(
    client: TestClient,
    clean_records: None,
) -> None:
    """Three rows at different days → count=3, first_event_at = earliest."""
    tenant = "tnt_us_mcp_status_three"
    _persist_mcp_row(tenant_id=tenant, days_old=0)
    _persist_mcp_row(tenant_id=tenant, days_old=2)
    _persist_mcp_row(tenant_id=tenant, days_old=5)
    response = client.get(
        "/onboarding/mcp-status",
        headers=_bearer(tenant_id=tenant),
    )
    body = response.json()
    assert body["configured"] is True
    assert body["events_count"] == 3
    # first_event_at is the EARLIEST — i.e., the days_old=5 row.
    first_at = datetime.fromisoformat(body["first_event_at"])
    age = datetime.now(timezone.utc) - first_at
    assert age > timedelta(days=4)
    assert age < timedelta(days=6)


def test_rows_older_than_90_days_are_excluded(
    client: TestClient,
    clean_records: None,
) -> None:
    """A row whose ingested_at is outside the 90-day window doesn't count.

    The endpoint filters on ``ingested_at > now() - INTERVAL '90 days'``,
    matching the capability detector's semantics ("recent activity = recent
    ingestion"). The persist task always stamps ingested_at via DB default
    (now()), so we backdate via raw SQL for the old row.
    """
    from vargate_telemetry.db import engine

    tenant = "tnt_us_mcp_status_old"
    zero32 = b"\x00" * 32  # placeholder 32-byte hash, schema-valid

    with engine.begin() as conn:
        conn.execute(sql_text("SET LOCAL ROLE vargate_app"))
        conn.execute(
            sql_text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": tenant},
        )
        # Old row — ingested 91 days ago. Should be filtered out.
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    subject_user_id, occurred_at, ingested_at,
                    content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, 'mcp_interaction', 'mcp', :old_ext_id,
                    'u', now() - INTERVAL '91 days',
                    now() - INTERVAL '91 days',
                    :h, '{}'::jsonb,
                    1, :h, :h
                )
                """
            ),
            {"t": tenant, "old_ext_id": "old-row-91d", "h": zero32},
        )
        # Fresh row — ingested today. Should be the only one that counts.
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    subject_user_id, occurred_at, ingested_at,
                    content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, 'mcp_interaction', 'mcp', :new_ext_id,
                    'u', now(), now(),
                    :h, '{}'::jsonb,
                    2, :h, :h
                )
                """
            ),
            {"t": tenant, "new_ext_id": "new-row-today", "h": zero32},
        )

    response = client.get(
        "/onboarding/mcp-status",
        headers=_bearer(tenant_id=tenant),
    )
    body = response.json()
    assert body["events_count"] == 1
