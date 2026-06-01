# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the budgets CRUD API (TM3 Phase B2).

Covers:
  - POST /api/budgets — happy path, each scope_kind, validation
    failures (scope_value mismatch, threshold ≤ 0, bad period,
    bad email), 400 when no tenant bound.
  - GET /api/budgets — list shape, excludes soft-deleted, RLS
    isolation across tenants.
  - GET /api/budgets/{id} — detail with current-period spend,
    404 on missing, 404 on soft-deleted.
  - PATCH /api/budgets/{id} — updates name / threshold / recipients,
    400 on empty body, 404 on missing.
  - DELETE /api/budgets/{id} — soft-delete sets deleted_at,
    404 second time.
  - GET /api/budget-alerts — list with / without unack filter.
  - POST /api/budget-alerts/{id}/acknowledge — happy + already-acked.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
def clean_budgets() -> Iterator[None]:
    """Empty budgets + alert events before AND after each test."""
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


def _provision_tenant(tenant_id: str, region: str = "us") -> None:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        # Upsert pattern — the tenants row may already exist from a
        # previous test's _provision_tenant() that we couldn't TRUNCATE
        # (FK constraints from users/etc.).
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


def _provision_user(tenant_id: str, role: str = "admin") -> str:
    """Create a real users row + return its UUID for use in the JWT.

    Budgets have a FK on ``created_by_user_id`` — without a matching
    users row, INSERT fails. Production flow always has the row
    (onboarding creates it); tests need to mirror that.

    TM4: budget create/update/delete are admin-gated, so the default
    auto-provisioned caller is an **admin** (every write test here
    assumes a writer). Pass ``role="member"`` to exercise the 403 gate.
    """
    user_uuid = str(uuid.uuid4())
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO users
                    (id, email, sso_provider, sso_subject_id, tenant_id, role)
                VALUES (:id, :email, 'google', :sub, :t, :role)
                """
            ),
            {
                "id": user_uuid,
                "email": f"probe-{user_uuid[:8]}@example.com",
                "sub": f"sub-{user_uuid}",
                "t": tenant_id,
                "role": role,
            },
        )
    return user_uuid


def _bearer(tenant_id: str | None, user_id: str | None = None) -> dict[str, str]:
    """JWT for a user bound to a tenant.

    If ``user_id`` is None and ``tenant_id`` is provided, a real
    users row is provisioned so the budget INSERT's FK holds.
    Pass ``user_id`` explicitly when a test needs to control the
    identity (e.g., to verify acknowledged_by_user_id matches).
    """
    from vargate_telemetry.auth.jwt import issue_session_jwt

    if user_id is None and tenant_id is not None:
        user_id = _provision_user(tenant_id)

    token = issue_session_jwt(
        user_id=user_id or str(uuid.uuid4()),
        email="probe@example.com",
        sso_provider="google",
        tenant_id=tenant_id,
    )
    return {"Authorization": f"Bearer {token}"}


# Sonnet rate: input $3/Mtok + output $15/Mtok.
# 1M input + 200k output = $6.00 — same fixture as test_budget_spend.
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
# POST /api/budgets
# ───────────────────────────────────────────────────────────────────────────


def test_create_tenant_scoped_budget_happy_path(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_budget_create_tenant"
    _provision_tenant(tenant)
    body = {
        "name": "Sera monthly cap",
        "scope_kind": "tenant",
        "scope_value": None,
        "period": "monthly",
        "threshold_usd": "500.00",
        "alert_recipients": ["rick@vargate.ai", "ops@vargate.ai"],
    }
    r = client.post("/budgets", json=body, headers=_bearer(tenant))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["name"] == "Sera monthly cap"
    assert out["scope_kind"] == "tenant"
    assert out["scope_value"] is None
    assert out["period"] == "monthly"
    assert Decimal(out["threshold_usd"]) == Decimal("500.00")
    assert out["alert_recipients"] == ["rick@vargate.ai", "ops@vargate.ai"]
    assert out["created_at"] is not None


def test_member_cannot_create_budget(
    clean_budgets: None, client: TestClient
) -> None:
    """TM4: budget writes are admin-gated. A member caller gets 403."""
    tenant = "tnt_us_budget_member_403"
    _provision_tenant(tenant)
    member = _provision_user(tenant, role="member")
    body = {
        "name": "should be rejected",
        "scope_kind": "tenant",
        "scope_value": None,
        "period": "monthly",
        "threshold_usd": "10.00",
        "alert_recipients": [],
    }
    r = client.post("/budgets", json=body, headers=_bearer(tenant, member))
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "admin_required"


def test_list_rows_carry_current_spend_and_ratio(
    clean_budgets: None, client: TestClient
) -> None:
    """TM4 polish: the roster rows now include current-period spend +
    ratio (detail-shaped) so the UI can render a progress bar."""
    tenant = "tnt_us_budget_list_spend"
    _provision_tenant(tenant)
    client.post(
        "/budgets",
        json={
            "name": "list spend cap",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "100.00",
            "alert_recipients": [],
        },
        headers=_bearer(tenant),
    )
    rows = client.get("/budgets", headers=_bearer(tenant)).json()["rows"]
    assert len(rows) == 1
    row = rows[0]
    # The progress-bar fields are present; with no usage seeded the
    # spend + ratio are zero (the computation itself is covered by the
    # detail tests).
    assert Decimal(row["current_spend_usd"]) == Decimal("0")
    assert Decimal(row["current_ratio"]) == Decimal("0")
    assert "current_period_start" in row
    assert row["current_threshold_crossed"] is None


def test_create_workspace_scoped_budget(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_budget_create_workspace"
    _provision_tenant(tenant)
    body = {
        "name": "Engineering ws weekly",
        "scope_kind": "workspace",
        "scope_value": "wrkspc_eng",
        "period": "weekly",
        "threshold_usd": "100.00",
        "alert_recipients": [],
    }
    r = client.post("/budgets", json=body, headers=_bearer(tenant))
    assert r.status_code == 201
    out = r.json()
    assert out["scope_kind"] == "workspace"
    assert out["scope_value"] == "wrkspc_eng"


def test_create_rejects_tenant_scope_with_value(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_budget_bad_pair_a"
    _provision_tenant(tenant)
    body = {
        "name": "bad",
        "scope_kind": "tenant",
        "scope_value": "should-be-null",
        "period": "monthly",
        "threshold_usd": "100.00",
    }
    r = client.post("/budgets", json=body, headers=_bearer(tenant))
    assert r.status_code == 422
    # Pydantic 2's error envelope nests under "detail".
    assert "scope" in r.text.lower()


def test_create_rejects_api_key_scope_without_value(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_budget_bad_pair_b"
    _provision_tenant(tenant)
    body = {
        "name": "bad",
        "scope_kind": "api_key",
        "scope_value": None,
        "period": "monthly",
        "threshold_usd": "100.00",
    }
    r = client.post("/budgets", json=body, headers=_bearer(tenant))
    assert r.status_code == 422


def test_create_rejects_zero_threshold(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_budget_zero"
    _provision_tenant(tenant)
    body = {
        "name": "bad",
        "scope_kind": "tenant",
        "scope_value": None,
        "period": "monthly",
        "threshold_usd": "0.00",
    }
    r = client.post("/budgets", json=body, headers=_bearer(tenant))
    assert r.status_code == 422


def test_create_rejects_invalid_email_recipient(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_budget_bad_email"
    _provision_tenant(tenant)
    body = {
        "name": "bad",
        "scope_kind": "tenant",
        "scope_value": None,
        "period": "monthly",
        "threshold_usd": "100.00",
        "alert_recipients": ["not-an-email"],
    }
    r = client.post("/budgets", json=body, headers=_bearer(tenant))
    assert r.status_code == 422


def test_create_rejects_no_tenant_bound(
    clean_budgets: None, client: TestClient
) -> None:
    body = {
        "name": "bad",
        "scope_kind": "tenant",
        "scope_value": None,
        "period": "monthly",
        "threshold_usd": "100.00",
    }
    r = client.post("/budgets", json=body, headers=_bearer(None))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "no_tenant_bound"


# ───────────────────────────────────────────────────────────────────────────
# GET /api/budgets — list
# ───────────────────────────────────────────────────────────────────────────


def test_list_returns_only_non_deleted(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_budget_list"
    _provision_tenant(tenant)
    auth = _bearer(tenant)

    a = client.post(
        "/budgets",
        json={
            "name": "Keep me",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "100.00",
        },
        headers=auth,
    ).json()
    b = client.post(
        "/budgets",
        json={
            "name": "Delete me",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "200.00",
        },
        headers=auth,
    ).json()
    # Soft-delete B.
    r = client.delete(f"/budgets/{b['id']}", headers=auth)
    assert r.status_code == 204

    r = client.get("/budgets", headers=auth)
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["id"] == a["id"]


def test_list_is_rls_isolated_across_tenants(
    clean_budgets: None, client: TestClient
) -> None:
    """A tenant's list MUST NOT include another tenant's budgets,
    even if the requesting JWT carries no scope to that tenant.
    RLS is the load-bearing check."""
    tenant_a = "tnt_us_budget_rls_a"
    tenant_b = "tnt_us_budget_rls_b"
    _provision_tenant(tenant_a)
    _provision_tenant(tenant_b)

    client.post(
        "/budgets",
        json={
            "name": "A's budget",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "100.00",
        },
        headers=_bearer(tenant_a),
    )
    client.post(
        "/budgets",
        json={
            "name": "B's budget",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "200.00",
        },
        headers=_bearer(tenant_b),
    )

    rows_a = client.get("/budgets", headers=_bearer(tenant_a)).json()["rows"]
    rows_b = client.get("/budgets", headers=_bearer(tenant_b)).json()["rows"]

    assert len(rows_a) == 1
    assert rows_a[0]["name"] == "A's budget"
    assert len(rows_b) == 1
    assert rows_b[0]["name"] == "B's budget"


# ───────────────────────────────────────────────────────────────────────────
# GET /api/budgets/{id} — detail with current spend
# ───────────────────────────────────────────────────────────────────────────


def test_detail_returns_current_period_spend_and_ratio(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_budget_detail_spend"
    _provision_tenant(tenant)
    auth = _bearer(tenant)

    # Seed $6 of usage WITHIN the current monthly period (today).
    _seed_usage_record(
        tenant,
        occurred_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
    )

    # $100 monthly tenant-wide budget → ratio is 6/100 = 0.06.
    created = client.post(
        "/budgets",
        json={
            "name": "Tight monthly",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "100.00",
        },
        headers=auth,
    ).json()

    r = client.get(f"/budgets/{created['id']}", headers=auth)
    assert r.status_code == 200
    out = r.json()
    assert Decimal(out["current_spend_usd"]) == Decimal("6.00")
    assert Decimal(out["current_ratio"]) == Decimal("0.0600")
    # 6% is below the lowest threshold; no threshold has been crossed.
    assert out["current_threshold_crossed"] is None
    assert out["current_period_start"]
    assert out["current_period_end"]


def test_detail_ratio_can_exceed_one(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_budget_over"
    _provision_tenant(tenant)
    auth = _bearer(tenant)

    _seed_usage_record(
        tenant,
        occurred_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
    )
    # $1 monthly cap; $6 spent → ratio = 6.0, threshold_crossed = 1.00.
    created = client.post(
        "/budgets",
        json={
            "name": "Tiny cap",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "1.00",
        },
        headers=auth,
    ).json()
    out = client.get(f"/budgets/{created['id']}", headers=auth).json()
    assert Decimal(out["current_ratio"]) >= Decimal("6.0000")
    assert Decimal(out["current_threshold_crossed"]) == Decimal("1.00")


def test_detail_404_on_unknown_id(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_budget_detail_404"
    _provision_tenant(tenant)
    r = client.get(
        f"/budgets/{uuid.uuid4()}", headers=_bearer(tenant)
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "budget_not_found"


def test_detail_404_on_soft_deleted(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_budget_detail_soft"
    _provision_tenant(tenant)
    auth = _bearer(tenant)
    created = client.post(
        "/budgets",
        json={
            "name": "delete me",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "100.00",
        },
        headers=auth,
    ).json()
    client.delete(f"/budgets/{created['id']}", headers=auth)
    r = client.get(f"/budgets/{created['id']}", headers=auth)
    assert r.status_code == 404


# ───────────────────────────────────────────────────────────────────────────
# PATCH /api/budgets/{id}
# ───────────────────────────────────────────────────────────────────────────


def test_patch_updates_threshold(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_budget_patch_threshold"
    _provision_tenant(tenant)
    auth = _bearer(tenant)
    created = client.post(
        "/budgets",
        json={
            "name": "raise me",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "50.00",
        },
        headers=auth,
    ).json()
    r = client.patch(
        f"/budgets/{created['id']}",
        json={"threshold_usd": "150.00"},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["threshold_usd"]) == Decimal("150.00")
    # Other fields untouched.
    assert r.json()["name"] == "raise me"


def test_patch_updates_recipients(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_budget_patch_recipients"
    _provision_tenant(tenant)
    auth = _bearer(tenant)
    created = client.post(
        "/budgets",
        json={
            "name": "swap recipients",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "50.00",
            "alert_recipients": ["one@vargate.ai"],
        },
        headers=auth,
    ).json()
    r = client.patch(
        f"/budgets/{created['id']}",
        json={"alert_recipients": ["two@vargate.ai", "three@vargate.ai"]},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json()["alert_recipients"] == [
        "two@vargate.ai",
        "three@vargate.ai",
    ]


def test_patch_empty_body_400(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_budget_patch_empty"
    _provision_tenant(tenant)
    auth = _bearer(tenant)
    created = client.post(
        "/budgets",
        json={
            "name": "patch nothing",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "50.00",
        },
        headers=auth,
    ).json()
    r = client.patch(f"/budgets/{created['id']}", json={}, headers=auth)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "no_fields_to_update"


# ───────────────────────────────────────────────────────────────────────────
# DELETE /api/budgets/{id}
# ───────────────────────────────────────────────────────────────────────────


def test_delete_is_idempotent_on_first_call_only(
    clean_budgets: None, client: TestClient
) -> None:
    """First DELETE returns 204, second returns 404 (already soft-
    deleted). The row stays in the table — the audit ledger never
    forgets an alert-history-bearing budget."""
    tenant = "tnt_us_budget_delete_twice"
    _provision_tenant(tenant)
    auth = _bearer(tenant)
    created = client.post(
        "/budgets",
        json={
            "name": "delete-me",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "100.00",
        },
        headers=auth,
    ).json()
    r1 = client.delete(f"/budgets/{created['id']}", headers=auth)
    assert r1.status_code == 204
    r2 = client.delete(f"/budgets/{created['id']}", headers=auth)
    assert r2.status_code == 404


# ───────────────────────────────────────────────────────────────────────────
# Budget alerts
# ───────────────────────────────────────────────────────────────────────────


def _seed_alert(
    tenant_id: str,
    budget_id: str,
    *,
    threshold: Decimal,
    spend: Decimal,
    acknowledged: bool = False,
) -> str:
    from vargate_telemetry.db import session_scope

    with session_scope(tenant_id) as s:
        row = s.execute(
            sql_text(
                """
                INSERT INTO budget_alert_events (
                    budget_id, tenant_id, period_start,
                    threshold_crossed, current_spend_usd,
                    acknowledged_at
                ) VALUES (
                    :budget_id, :tenant_id, :period_start,
                    :threshold, :spend,
                    CASE WHEN :ack THEN now() ELSE NULL END
                )
                RETURNING id::text
                """
            ),
            {
                "budget_id": budget_id,
                "tenant_id": tenant_id,
                "period_start": datetime.now(tz=timezone.utc).date(),
                "threshold": threshold,
                "spend": spend,
                "ack": acknowledged,
            },
        ).one()
    return row.id


def test_list_alerts_returns_both_acked_and_unacked(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_alerts_list"
    _provision_tenant(tenant)
    auth = _bearer(tenant)
    created = client.post(
        "/budgets",
        json={
            "name": "for alerts",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "100.00",
        },
        headers=auth,
    ).json()
    _seed_alert(
        tenant,
        created["id"],
        threshold=Decimal("0.70"),
        spend=Decimal("70.00"),
    )
    _seed_alert(
        tenant,
        created["id"],
        threshold=Decimal("0.85"),
        spend=Decimal("85.00"),
        acknowledged=True,
    )

    rows = client.get("/budget-alerts", headers=auth).json()["rows"]
    assert len(rows) == 2
    # Joined budget name should be present.
    assert all(r["budget_name"] == "for alerts" for r in rows)


def test_list_alerts_unack_filter_excludes_acked(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_alerts_unack"
    _provision_tenant(tenant)
    auth = _bearer(tenant)
    created = client.post(
        "/budgets",
        json={
            "name": "for alerts",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "100.00",
        },
        headers=auth,
    ).json()
    _seed_alert(
        tenant,
        created["id"],
        threshold=Decimal("0.70"),
        spend=Decimal("70.00"),
    )
    _seed_alert(
        tenant,
        created["id"],
        threshold=Decimal("0.85"),
        spend=Decimal("85.00"),
        acknowledged=True,
    )

    rows = client.get(
        "/budget-alerts?unack=true", headers=auth
    ).json()["rows"]
    assert len(rows) == 1
    assert rows[0]["acknowledged_at"] is None


def test_acknowledge_alert_happy_path(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_alerts_ack_ok"
    user_uuid = str(uuid.uuid4())
    _provision_tenant(tenant)
    # Need a real user row so the FK on acknowledged_by_user_id holds.
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO users
                    (id, email, sso_provider, sso_subject_id, tenant_id, role)
                VALUES (:id, :email, 'google', :sub, :t, 'admin')
                """
            ),
            {
                "id": user_uuid,
                "email": "ack@example.com",
                "sub": f"sub-{user_uuid}",
                "t": tenant,
            },
        )

    auth = _bearer(tenant, user_id=user_uuid)
    created = client.post(
        "/budgets",
        json={
            "name": "to be acked",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "100.00",
        },
        headers=auth,
    ).json()
    alert_id = _seed_alert(
        tenant,
        created["id"],
        threshold=Decimal("0.70"),
        spend=Decimal("70.00"),
    )
    r = client.post(
        f"/budget-alerts/{alert_id}/acknowledge", headers=auth
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["acknowledged_at"] is not None
    assert out["acknowledged_by_user_id"] == user_uuid


def test_acknowledge_alert_404_when_already_acked(
    clean_budgets: None, client: TestClient
) -> None:
    tenant = "tnt_us_alerts_ack_twice"
    _provision_tenant(tenant)
    auth = _bearer(tenant)
    created = client.post(
        "/budgets",
        json={
            "name": "double ack",
            "scope_kind": "tenant",
            "scope_value": None,
            "period": "monthly",
            "threshold_usd": "100.00",
        },
        headers=auth,
    ).json()
    alert_id = _seed_alert(
        tenant,
        created["id"],
        threshold=Decimal("0.70"),
        spend=Decimal("70.00"),
        acknowledged=True,
    )
    r = client.post(
        f"/budget-alerts/{alert_id}/acknowledge", headers=auth
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "alert_not_found_or_already_acked"
