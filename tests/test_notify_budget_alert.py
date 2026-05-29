# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the budget-alert template + send wrapper (TM3 Phase B4).

Two layers:

1. ``render_budget_alert`` — pure formatter. Asserts the subject /
   text / html contain the right values for known contexts.
2. ``send_budget_alert`` — calls ``email.send_email`` under the
   hood; we monkey-patch the SES seam so no network call happens.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from vargate_telemetry.notify import email as email_mod
from vargate_telemetry.notify.budget_alert import (
    BudgetAlertContext,
    render_budget_alert,
    send_budget_alert,
)
from vargate_telemetry.notify.email import EmailDeliveryError


@pytest.fixture(autouse=True)
def reset_ses_client() -> None:
    email_mod._reset_client_for_test()


def _ctx(
    *,
    threshold: Decimal = Decimal("0.70"),
    threshold_usd: Decimal = Decimal("100.00"),
    spend: Decimal = Decimal("70.00"),
    scope_kind: str = "tenant",
    scope_value: str | None = None,
    period: str = "monthly",
) -> BudgetAlertContext:
    return BudgetAlertContext(
        budget_name="Sera prod monthly",
        scope_kind=scope_kind,
        scope_label=(
            "All Anthropic API usage for this tenant"
            if scope_kind == "tenant"
            else f"{scope_kind} = {scope_value}"
        ),
        period=period,
        period_start=date(2026, 5, 1),
        period_end=date(2026, 6, 1),
        threshold_crossed=threshold,
        threshold_usd=threshold_usd,
        current_spend_usd=spend,
    )


# ───────────────────────────────────────────────────────────────────────────
# Formatter
# ───────────────────────────────────────────────────────────────────────────


def test_subject_includes_budget_name_and_threshold_percent() -> None:
    subj, _, _ = render_budget_alert(_ctx(threshold=Decimal("0.70")))
    assert "Sera prod monthly" in subj
    assert "70%" in subj
    # No product prefix — the From + branded template carry identity.
    assert "[Ogma]" not in subj
    assert subj.startswith("Budget alert")


def test_subject_renders_100_percent_for_max_threshold() -> None:
    subj, _, _ = render_budget_alert(_ctx(threshold=Decimal("1.00")))
    assert "100%" in subj


def test_text_body_includes_spend_threshold_period_and_dashboard_link() -> None:
    _, _, text = render_budget_alert(
        _ctx(spend=Decimal("85.23"), threshold_usd=Decimal("100.00"))
    )
    assert "$85.23" in text
    assert "$100.00" in text
    assert "2026-05-01" in text
    assert "2026-06-01" in text
    assert "ogma.vargate.ai/alerts" in text  # Default URL.


def test_html_body_is_well_formed_and_includes_cta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OGMA_DASHBOARD_URL", "https://dev.example.com")
    _, html, _ = render_budget_alert(_ctx())
    assert "<html>" in html and "</html>" in html
    # CTA goes to env-configured dashboard URL.
    assert "https://dev.example.com/alerts" in html
    # No remote font / image asset shenanigans — pure inline styles.
    assert "googleapis.com" not in html
    assert "<img" not in html


def test_scope_label_for_workspace_includes_kind_and_value() -> None:
    _, _, text = render_budget_alert(
        _ctx(scope_kind="workspace", scope_value="wrkspc_eng")
    )
    assert "workspace = wrkspc_eng" in text


# ───────────────────────────────────────────────────────────────────────────
# send_budget_alert wrapper
# ───────────────────────────────────────────────────────────────────────────


def test_send_budget_alert_noops_on_empty_recipient_list() -> None:
    """Empty recipients is valid configuration — wrapper returns
    None without raising and without calling SES."""
    result = send_budget_alert(recipients=[], ctx=_ctx())
    assert result is None


def test_send_budget_alert_dispatches_to_ses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OGMA_ALERT_FROM_ADDRESS", "alerts@vargate.ai")
    mock_client = MagicMock()
    mock_client.send_email.return_value = {"MessageId": "id-xyz"}
    monkeypatch.setattr(
        email_mod, "_build_ses_client", lambda: mock_client
    )

    msg_id = send_budget_alert(
        recipients=["rick@vargate.ai", "ops@vargate.ai"],
        ctx=_ctx(),
    )
    assert msg_id == "id-xyz"
    args, kwargs = mock_client.send_email.call_args
    assert kwargs["Destination"]["ToAddresses"] == [
        "rick@vargate.ai",
        "ops@vargate.ai",
    ]
    assert "Sera prod monthly" in kwargs["Message"]["Subject"]["Data"]


def test_send_budget_alert_propagates_email_delivery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OGMA_ALERT_FROM_ADDRESS", "alerts@vargate.ai")
    mock_client = MagicMock()
    mock_client.send_email.side_effect = RuntimeError("oops")
    monkeypatch.setattr(
        email_mod, "_build_ses_client", lambda: mock_client
    )

    with pytest.raises(EmailDeliveryError):
        send_budget_alert(
            recipients=["rick@example.com"], ctx=_ctx()
        )
