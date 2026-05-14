# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the budget-alert evaluator (TM3 Phase B3).

End-to-end: seed a tenant + a budget + admin-usage records, run
``evaluate_budgets_for_tenant``, assert
  - the right alert_event rows landed
  - the right SES calls happened
  - a second tick is a silent no-op (dedup)
  - threshold-raising mid-period stops re-firing the lower
    threshold from a previous period without affecting later
    periods
  - email delivery failure does NOT roll back the alert row
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterator
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text as sql_text

from vargate_telemetry.notify import email as email_mod
from vargate_telemetry.tasks.evaluate_budgets import (
    evaluate_budgets_for_tenant,
)


os.environ.setdefault(
    "JWT_SIGNING_KEY",
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b",
)


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_ses_client() -> None:
    email_mod._reset_client_for_test()


@pytest.fixture
def mock_ses(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Install a mock boto3 SES client + set the from-address env."""
    monkeypatch.setenv("OGMA_ALERT_FROM_ADDRESS", "alerts@vargate.ai")
    client = MagicMock()
    client.send_email.return_value = {"MessageId": "mock-id"}
    monkeypatch.setattr(email_mod, "_build_ses_client", lambda: client)
    return client


@pytest.fixture
def clean_state() -> Iterator[None]:
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


def _provision_tenant_and_user(tenant_id: str) -> str:
    """Provision a real tenant + user. Returns user_uuid."""
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


def _create_budget(
    tenant_id: str,
    *,
    user_uuid: str,
    name: str = "Test monthly",
    scope_kind: str = "tenant",
    scope_value: str | None = None,
    period: str = "monthly",
    threshold_usd: Decimal = Decimal("100.00"),
    recipients: list[str] | None = None,
) -> str:
    """INSERT a budgets row directly; return its id."""
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
                    :t, :name, :scope_kind, :scope_value,
                    :period, :threshold, :recipients,
                    :user_uuid
                )
                RETURNING id::text
                """
            ),
            {
                "t": tenant_id,
                "name": name,
                "scope_kind": scope_kind,
                "scope_value": scope_value,
                "period": period,
                "threshold": threshold_usd,
                "recipients": recipients or [],
                "user_uuid": user_uuid,
            },
        ).one()
    return row.id


# Sonnet rate: $3/Mtok in + $15/Mtok out. 1M in + 200k out = $6.00.
_SONNET = "claude-sonnet-4-5-20250929"


def _seed_usage(
    tenant_id: str,
    *,
    occurred_at: datetime,
    input_tokens: int,
    output_tokens: int = 0,
    model: str = _SONNET,
    workspace_id: str | None = None,
    api_key_id: str | None = None,
) -> None:
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


def _count_alerts(tenant_id: str, budget_id: str) -> int:
    from vargate_telemetry.db import session_scope

    with session_scope(tenant_id) as s:
        row = s.execute(
            sql_text(
                "SELECT COUNT(*) AS n FROM budget_alert_events "
                "WHERE budget_id = :b"
            ),
            {"b": budget_id},
        ).one()
    return int(row.n)


# ───────────────────────────────────────────────────────────────────────────
# Cases
# ───────────────────────────────────────────────────────────────────────────


def test_evaluate_with_no_budgets_is_noop(
    clean_state: None, mock_ses: MagicMock
) -> None:
    tenant = "tnt_us_eval_noop"
    _provision_tenant_and_user(tenant)
    result = evaluate_budgets_for_tenant(tenant)
    assert result == {
        "tenant_id": tenant,
        "budgets_checked": 0,
        "thresholds_fired": [],
    }
    mock_ses.send_email.assert_not_called()


def test_below_first_threshold_fires_nothing(
    clean_state: None, mock_ses: MagicMock
) -> None:
    tenant = "tnt_us_eval_below"
    user = _provision_tenant_and_user(tenant)
    # $6 spend vs $100 threshold = 6% ratio — well below 70%.
    _seed_usage(
        tenant,
        occurred_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    budget_id = _create_budget(
        tenant,
        user_uuid=user,
        threshold_usd=Decimal("100.00"),
        recipients=["rick@vargate.ai"],
    )

    result = evaluate_budgets_for_tenant(tenant)
    assert result["budgets_checked"] == 1
    assert result["thresholds_fired"] == []
    assert _count_alerts(tenant, budget_id) == 0
    mock_ses.send_email.assert_not_called()


def test_first_run_at_70pct_fires_one_alert_and_one_email(
    clean_state: None, mock_ses: MagicMock
) -> None:
    tenant = "tnt_us_eval_70"
    user = _provision_tenant_and_user(tenant)
    # $6 spend / $8 threshold = 75% — crosses 70 but not 85.
    _seed_usage(
        tenant,
        occurred_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    budget_id = _create_budget(
        tenant,
        user_uuid=user,
        threshold_usd=Decimal("8.00"),
        recipients=["rick@vargate.ai"],
    )

    result = evaluate_budgets_for_tenant(tenant)
    assert len(result["thresholds_fired"]) == 1
    assert result["thresholds_fired"][0].endswith(":0.70")
    assert _count_alerts(tenant, budget_id) == 1
    assert mock_ses.send_email.call_count == 1


def test_first_run_over_100pct_fires_three_alerts_three_emails(
    clean_state: None, mock_ses: MagicMock
) -> None:
    tenant = "tnt_us_eval_over"
    user = _provision_tenant_and_user(tenant)
    # $6 spend / $1 threshold = 600% — crosses all three thresholds.
    _seed_usage(
        tenant,
        occurred_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    budget_id = _create_budget(
        tenant,
        user_uuid=user,
        threshold_usd=Decimal("1.00"),
        recipients=["rick@vargate.ai"],
    )

    result = evaluate_budgets_for_tenant(tenant)
    assert len(result["thresholds_fired"]) == 3
    assert _count_alerts(tenant, budget_id) == 3
    assert mock_ses.send_email.call_count == 3


def test_second_run_within_same_period_is_silent_noop(
    clean_state: None, mock_ses: MagicMock
) -> None:
    """The 15-minute beat ticks frequently. After the first tick
    fires the alert, every subsequent tick within the same period
    must NOT fire again — that's the dedup contract."""
    tenant = "tnt_us_eval_dedup"
    user = _provision_tenant_and_user(tenant)
    _seed_usage(
        tenant,
        occurred_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    budget_id = _create_budget(
        tenant,
        user_uuid=user,
        threshold_usd=Decimal("1.00"),  # spend will be 600%
        recipients=["rick@vargate.ai"],
    )

    # First run: three alerts fire.
    evaluate_budgets_for_tenant(tenant)
    assert _count_alerts(tenant, budget_id) == 3
    assert mock_ses.send_email.call_count == 3

    # Second run, same data, same period — nothing new.
    result = evaluate_budgets_for_tenant(tenant)
    assert result["thresholds_fired"] == []
    assert _count_alerts(tenant, budget_id) == 3  # Unchanged.
    assert mock_ses.send_email.call_count == 3  # Unchanged.


def test_recipients_empty_records_alert_but_skips_ses(
    clean_state: None, mock_ses: MagicMock
) -> None:
    """A budget with no recipients should still record the alert
    event (the dashboard surfaces it) but NOT call SES."""
    tenant = "tnt_us_eval_no_recipients"
    user = _provision_tenant_and_user(tenant)
    _seed_usage(
        tenant,
        occurred_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    budget_id = _create_budget(
        tenant,
        user_uuid=user,
        threshold_usd=Decimal("8.00"),  # 75% — fires 0.70
        recipients=[],
    )

    evaluate_budgets_for_tenant(tenant)
    assert _count_alerts(tenant, budget_id) == 1  # Row recorded.
    mock_ses.send_email.assert_not_called()  # But no email.


def test_ses_failure_does_not_rollback_alert_row(
    clean_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If SES raises mid-send, the alert row MUST remain. The
    customer learns from the dashboard; we don't re-fire the same
    threshold on the next tick because of a transient SES blip."""
    tenant = "tnt_us_eval_ses_fail"
    user = _provision_tenant_and_user(tenant)

    monkeypatch.setenv("OGMA_ALERT_FROM_ADDRESS", "alerts@vargate.ai")
    mock_client = MagicMock()
    mock_client.send_email.side_effect = RuntimeError("SES is down")
    monkeypatch.setattr(
        email_mod, "_build_ses_client", lambda: mock_client
    )

    _seed_usage(
        tenant,
        occurred_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    budget_id = _create_budget(
        tenant,
        user_uuid=user,
        threshold_usd=Decimal("8.00"),
        recipients=["rick@vargate.ai"],
    )

    result = evaluate_budgets_for_tenant(tenant)
    # Threshold fired (the alert event row is the contract).
    assert len(result["thresholds_fired"]) == 1
    # Row landed despite SES failure.
    assert _count_alerts(tenant, budget_id) == 1
    # SES was tried.
    assert mock_client.send_email.call_count == 1


def test_ses_not_configured_records_alert_without_email(
    clean_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A misconfigured prod box (no OGMA_ALERT_FROM_ADDRESS) MUST
    still record alert events. The dashboard remains the source of
    truth; the customer sees the alert in-app."""
    tenant = "tnt_us_eval_no_ses"
    user = _provision_tenant_and_user(tenant)
    monkeypatch.delenv("OGMA_ALERT_FROM_ADDRESS", raising=False)

    _seed_usage(
        tenant,
        occurred_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    budget_id = _create_budget(
        tenant,
        user_uuid=user,
        threshold_usd=Decimal("8.00"),
        recipients=["rick@vargate.ai"],
    )

    result = evaluate_budgets_for_tenant(tenant)
    assert len(result["thresholds_fired"]) == 1
    assert _count_alerts(tenant, budget_id) == 1


def test_soft_deleted_budget_is_not_evaluated(
    clean_state: None, mock_ses: MagicMock
) -> None:
    tenant = "tnt_us_eval_soft_deleted"
    user = _provision_tenant_and_user(tenant)
    _seed_usage(
        tenant,
        occurred_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    budget_id = _create_budget(
        tenant,
        user_uuid=user,
        threshold_usd=Decimal("1.00"),
        recipients=["rick@vargate.ai"],
    )
    # Soft-delete the budget.
    from vargate_telemetry.db import session_scope

    with session_scope(tenant) as s:
        s.execute(
            sql_text(
                "UPDATE budgets SET deleted_at = now() WHERE id = :id"
            ),
            {"id": budget_id},
        )

    result = evaluate_budgets_for_tenant(tenant)
    assert result["budgets_checked"] == 0
    mock_ses.send_email.assert_not_called()
