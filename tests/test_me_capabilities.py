# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""TM2 Phase D2 — GET /me/capabilities tests.

State-of-tenant capability snapshot. Each bool answers "does the
tenant have at least one telemetry_records row with the matching
source_api in the last 90 days." Same uniform semantics as the
``mcp_connector`` detector in onboarding.py.

Cases:

  - No session → 401.
  - Pre-tenant user (no tenant_id) → all five False.
  - Tenant with no data → all five False.
  - Tenant with only mcp rows → mcp_connector True, the rest False.
  - Tenant with admin + activity_feed + code_analytics + mcp rows
    → four True, content_capture False (T5.3 invariant).
  - content_capture is ALWAYS False, even if you somehow plant a
    row with source_api='compliance_content' (it's not a runtime
    bool, just a placeholder).
"""

from __future__ import annotations

from typing import Iterator

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


def _bearer(*, tenant_id: str | None = "tnt_us_capabilities_test") -> dict[str, str]:
    from vargate_telemetry.auth.jwt import issue_session_jwt

    token = issue_session_jwt(
        user_id="user-capabilities-test",
        email="capabilities@example.com",
        sso_provider="google",
        tenant_id=tenant_id,
    )
    return {"Authorization": f"Bearer {token}"}


def _insert_record(
    *,
    tenant_id: str,
    source_api: str,
    chain_seq: int,
    days_old: int = 0,
) -> None:
    """Raw SQL insert — bypasses the chain primitive so we can plant
    records for any source_api without going through pull tasks."""
    from vargate_telemetry.db import engine

    zero32 = b"\x00" * 32

    with engine.begin() as conn:
        conn.execute(sql_text("SET LOCAL ROLE vargate_app"))
        conn.execute(
            sql_text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": tenant_id},
        )
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    subject_user_id, occurred_at, ingested_at,
                    content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, 'cap_test', :s, :ext,
                    'u',
                    now() - (:days * INTERVAL '1 day'),
                    now() - (:days * INTERVAL '1 day'),
                    :h, '{}'::jsonb,
                    :seq, :h, :h
                )
                """
            ),
            {
                "t": tenant_id,
                "s": source_api,
                "ext": f"cap-test-{source_api}-{chain_seq}",
                "days": days_old,
                "h": zero32,
                "seq": chain_seq,
            },
        )


# ───────────────────────────────────────────────────────────────────────────
# Auth + empty cases
# ───────────────────────────────────────────────────────────────────────────


def test_no_session_returns_401(client: TestClient) -> None:
    response = client.get("/me/capabilities")
    assert response.status_code == 401


def test_pre_tenant_user_gets_all_false(
    client: TestClient,
    clean_records: None,
) -> None:
    """A mid-onboarding caller (tenant_id=None) gets every bool False."""
    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=None)
    )
    assert response.status_code == 200
    assert response.json() == {
        "admin_api": False,
        "activity_feed": False,
        "content_capture": False,
        "code_analytics": False,
        "mcp_connector": False,
    }


def test_tenant_with_no_data_gets_all_false(
    client: TestClient,
    clean_records: None,
) -> None:
    """A tenant exists but has no rows yet — every bool False."""
    response = client.get(
        "/me/capabilities",
        headers=_bearer(tenant_id="tnt_us_empty_cap"),
    )
    body = response.json()
    assert body["admin_api"] is False
    assert body["activity_feed"] is False
    assert body["content_capture"] is False
    assert body["code_analytics"] is False
    assert body["mcp_connector"] is False


# ───────────────────────────────────────────────────────────────────────────
# Populated tenants
# ───────────────────────────────────────────────────────────────────────────


def test_only_mcp_rows_lights_mcp_connector(
    client: TestClient,
    clean_records: None,
) -> None:
    """A tenant whose only ingest path is MCP gets mcp_connector=True only."""
    tenant = "tnt_us_only_mcp"
    _insert_record(tenant_id=tenant, source_api="mcp", chain_seq=1)
    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=tenant)
    )
    body = response.json()
    assert body["mcp_connector"] is True
    assert body["admin_api"] is False
    assert body["activity_feed"] is False
    assert body["code_analytics"] is False


def test_all_four_sources_light_their_capabilities(
    client: TestClient,
    clean_records: None,
) -> None:
    """Admin + activity_feed + code_analytics + mcp rows → four True,
    content_capture stays False (T5.3 invariant)."""
    tenant = "tnt_us_all_four"
    _insert_record(tenant_id=tenant, source_api="admin", chain_seq=1)
    _insert_record(
        tenant_id=tenant, source_api="compliance_activities", chain_seq=2
    )
    _insert_record(
        tenant_id=tenant, source_api="code_analytics", chain_seq=3
    )
    _insert_record(tenant_id=tenant, source_api="mcp", chain_seq=4)

    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=tenant)
    )
    body = response.json()
    assert body["admin_api"] is True
    assert body["activity_feed"] is True
    assert body["code_analytics"] is True
    assert body["mcp_connector"] is True
    # T5.3 invariant: even with all four real sources active,
    # content_capture is always False.
    assert body["content_capture"] is False


def test_content_capture_is_always_false(
    client: TestClient,
    clean_records: None,
) -> None:
    """Even an attacker-planted compliance_content row doesn't flip
    content_capture — it's a hardcoded False in the endpoint.

    The reserved-field semantics from T5.3 say content_capture
    requires the future Compliance Access Key onboarding step;
    until that ships, the endpoint MUST return False regardless
    of what's in telemetry_records.
    """
    tenant = "tnt_us_content_attempt"
    _insert_record(
        tenant_id=tenant, source_api="compliance_content", chain_seq=1
    )
    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=tenant)
    )
    assert response.json()["content_capture"] is False


def test_old_rows_dont_count(
    client: TestClient,
    clean_records: None,
) -> None:
    """A 91-day-old row is outside the recent-activity window."""
    tenant = "tnt_us_old_only"
    _insert_record(
        tenant_id=tenant, source_api="admin", chain_seq=1, days_old=91
    )
    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=tenant)
    )
    assert response.json()["admin_api"] is False
