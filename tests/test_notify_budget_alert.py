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
from vargate_telemetry.notify import pagerduty as pagerduty_mod
from vargate_telemetry.notify import slack as slack_mod
from vargate_telemetry.notify.budget_alert import (
    BudgetAlertContext,
    render_budget_alert,
    send_budget_alert,
)
from vargate_telemetry.notify.email import EmailDeliveryError
from vargate_telemetry.notify.pagerduty import render_pagerduty_event
from vargate_telemetry.notify.slack import render_slack_alert


def _ok_response() -> MagicMock:
    """A fake httpx.Response that passes raise_for_status()."""
    r = MagicMock()
    r.raise_for_status.return_value = None
    return r


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
    # TM4 polish: branded "Ogma by Vargate" header + footer.
    assert "Ogma" in html
    assert "by Vargate" in html


def test_scope_label_for_workspace_includes_kind_and_value() -> None:
    _, _, text = render_budget_alert(
        _ctx(scope_kind="workspace", scope_value="wrkspc_eng")
    )
    assert "workspace = wrkspc_eng" in text


# ───────────────────────────────────────────────────────────────────────────
# send_budget_alert wrapper
# ───────────────────────────────────────────────────────────────────────────


def _stub_ses(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> MagicMock:
    monkeypatch.setenv("OGMA_ALERT_FROM_ADDRESS", "alerts@vargate.ai")
    mock_client = MagicMock()
    if fail:
        mock_client.send_email.side_effect = RuntimeError("oops")
    else:
        mock_client.send_email.return_value = {"MessageId": "id-xyz"}
    monkeypatch.setattr(email_mod, "_build_ses_client", lambda: mock_client)
    return mock_client


def test_send_budget_alert_noops_on_empty_config() -> None:
    """No recipients on any channel is valid config — returns an empty
    summary, no raise, no I/O. (Bare empty list = email-only-empty too.)"""
    assert send_budget_alert(recipients={}, ctx=_ctx()) == {}
    assert send_budget_alert(recipients=[], ctx=_ctx()) == {}


def test_send_budget_alert_email_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = _stub_ses(monkeypatch)
    summary = send_budget_alert(
        recipients={"email": ["rick@vargate.ai", "ops@vargate.ai"]},
        ctx=_ctx(),
    )
    assert summary["email"]["status"] == "ok"
    assert summary["email"]["message_id"] == "id-xyz"
    assert "slack" not in summary and "pagerduty" not in summary
    _, kwargs = mock_client.send_email.call_args
    assert kwargs["Destination"]["ToAddresses"] == [
        "rick@vargate.ai",
        "ops@vargate.ai",
    ]


def test_send_budget_alert_accepts_legacy_email_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare list is still accepted as email-only (back-compat)."""
    _stub_ses(monkeypatch)
    summary = send_budget_alert(recipients=["rick@vargate.ai"], ctx=_ctx())
    assert summary["email"]["status"] == "ok"


def test_send_budget_alert_email_error_recorded_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SES failure is caught + recorded in the summary, NOT raised —
    a notify failure must not roll back the evaluator's alert row."""
    _stub_ses(monkeypatch, fail=True)
    summary = send_budget_alert(recipients={"email": ["x@y.com"]}, ctx=_ctx())
    assert summary["email"]["status"] == "error"


def test_send_budget_alert_dispatches_slack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    monkeypatch.setattr(
        slack_mod,
        "_post",
        lambda url, payload: calls.append((url, payload)) or _ok_response(),
    )
    summary = send_budget_alert(
        recipients={"slack_webhook": ["https://hooks.slack.com/services/T/B/x"]},
        ctx=_ctx(),
    )
    assert summary["slack"][0]["status"] == "ok"
    assert calls[0][0] == "https://hooks.slack.com/services/T/B/x"
    assert "blocks" in calls[0][1]  # Block Kit payload sent


def test_send_budget_alert_dispatches_pagerduty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    monkeypatch.setattr(
        pagerduty_mod,
        "_post",
        lambda payload: calls.append(payload) or _ok_response(),
    )
    summary = send_budget_alert(
        recipients={"pagerduty_key": ["routingkey0123456789"]},
        ctx=_ctx(threshold=Decimal("1.00")),
    )
    assert summary["pagerduty"][0]["status"] == "ok"
    assert calls[0]["routing_key"] == "routingkey0123456789"
    assert calls[0]["event_action"] == "trigger"
    assert calls[0]["payload"]["severity"] == "critical"  # 100% -> critical


def test_send_budget_alert_channels_are_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing Slack webhook must not stop email or PagerDuty."""
    _stub_ses(monkeypatch)

    def _slack_boom(url, payload):
        raise RuntimeError("slack 404")

    monkeypatch.setattr(slack_mod, "_post", _slack_boom)
    monkeypatch.setattr(pagerduty_mod, "_post", lambda payload: _ok_response())

    summary = send_budget_alert(
        recipients={
            "email": ["x@y.com"],
            "slack_webhook": ["https://hooks.slack.com/services/T/B/x"],
            "pagerduty_key": ["routingkey0123456789"],
        },
        ctx=_ctx(),
    )
    assert summary["email"]["status"] == "ok"
    assert summary["slack"][0]["status"] == "error"  # isolated failure
    assert summary["pagerduty"][0]["status"] == "ok"


# ───────────────────────────────────────────────────────────────────────────
# Channel renderers (pure)
# ───────────────────────────────────────────────────────────────────────────


def test_render_slack_alert_has_text_fallback_and_blocks() -> None:
    payload = render_slack_alert(_ctx(threshold=Decimal("0.85")))
    assert "Sera prod monthly" in payload["text"]
    assert "85%" in payload["text"]
    assert isinstance(payload["blocks"], list) and payload["blocks"]


def test_render_pagerduty_event_shape_and_severity() -> None:
    warn = render_pagerduty_event("rk", _ctx(threshold=Decimal("0.70")))
    assert warn["event_action"] == "trigger"
    assert warn["routing_key"] == "rk"
    assert warn["payload"]["severity"] == "warning"
    assert "Sera prod monthly" in warn["payload"]["summary"]
    # dedup_key is stable per (budget, period, threshold).
    crit = render_pagerduty_event("rk", _ctx(threshold=Decimal("1.00")))
    assert crit["payload"]["severity"] == "critical"
    assert warn["dedup_key"] != crit["dedup_key"]
