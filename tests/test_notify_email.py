# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the AWS SES email wrapper (TM3 Phase B4).

The boto3 client is monkey-patched at the build seam so no test
ever reaches real AWS. Two seams matter:

1. ``_build_ses_client`` — replaced with a mock factory that returns
   a fake client with a stub ``send_email``.
2. ``_reset_client_for_test`` — drops the module's cached client so
   the mock factory is consulted on the next call.

We do NOT test the boto3 internals (their tests cover that). We
test:
  - send_email raises SesNotConfigured when from-address env unset
  - send_email raises ValueError on empty to-list
  - send_email returns MessageId on success
  - send_email raises EmailDeliveryError when boto3 raises
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from vargate_telemetry.notify import email as email_mod
from vargate_telemetry.notify.email import (
    EmailDeliveryError,
    SesNotConfigured,
    send_email,
)


@pytest.fixture(autouse=True)
def reset_client() -> None:
    """Drop the cached boto3 SES client before each test so module
    state doesn't leak between cases."""
    email_mod._reset_client_for_test()


def _install_mock_ses(monkeypatch: pytest.MonkeyPatch, mock_client: MagicMock) -> None:
    """Replace _build_ses_client so it returns the mock."""
    monkeypatch.setattr(
        email_mod, "_build_ses_client", lambda: mock_client
    )


def test_send_email_returns_message_id_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OGMA_ALERT_FROM_ADDRESS", "alerts@vargate.ai")
    mock_client = MagicMock()
    mock_client.send_email.return_value = {"MessageId": "ses-abc-123"}
    _install_mock_ses(monkeypatch, mock_client)

    msg_id = send_email(
        to=["rick@example.com"],
        subject="hi",
        html_body="<p>hi</p>",
        text_body="hi",
    )
    assert msg_id == "ses-abc-123"
    # Verify the wire shape the wrapper sends to SES. Source carries
    # the "Vargate.ai" display name (formataddr quotes the dotted
    # name) wrapping the env sender address.
    args, kwargs = mock_client.send_email.call_args
    assert kwargs["Source"] == '"Vargate.ai" <alerts@vargate.ai>'
    assert kwargs["Destination"] == {"ToAddresses": ["rick@example.com"]}
    assert kwargs["Message"]["Subject"]["Data"] == "hi"
    assert kwargs["Message"]["Body"]["Html"]["Data"] == "<p>hi</p>"
    assert kwargs["Message"]["Body"]["Text"]["Data"] == "hi"


def test_send_email_raises_when_from_address_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OGMA_ALERT_FROM_ADDRESS", raising=False)
    mock_client = MagicMock()
    _install_mock_ses(monkeypatch, mock_client)

    with pytest.raises(SesNotConfigured, match="OGMA_ALERT_FROM_ADDRESS"):
        send_email(
            to=["rick@example.com"],
            subject="hi",
            html_body="<p>hi</p>",
            text_body="hi",
        )
    # And the SES client must NOT have been hit.
    mock_client.send_email.assert_not_called()


def test_send_email_rejects_empty_recipient_list() -> None:
    with pytest.raises(ValueError, match="non-empty list"):
        send_email(
            to=[],
            subject="hi",
            html_body="<p>hi</p>",
            text_body="hi",
        )


def test_send_email_raises_email_delivery_error_on_boto3_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OGMA_ALERT_FROM_ADDRESS", "alerts@vargate.ai")
    mock_client = MagicMock()
    mock_client.send_email.side_effect = RuntimeError("SES is on fire")
    _install_mock_ses(monkeypatch, mock_client)

    with pytest.raises(EmailDeliveryError, match="SES is on fire"):
        send_email(
            to=["rick@example.com"],
            subject="hi",
            html_body="<p>hi</p>",
            text_body="hi",
        )


def test_send_email_raises_when_ses_response_has_no_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OGMA_ALERT_FROM_ADDRESS", "alerts@vargate.ai")
    mock_client = MagicMock()
    # Some SES error modes return a 200 with an empty body — defensive
    # check that we don't silently call success on a missing MessageId.
    mock_client.send_email.return_value = {}
    _install_mock_ses(monkeypatch, mock_client)

    with pytest.raises(EmailDeliveryError, match="missing MessageId"):
        send_email(
            to=["rick@example.com"],
            subject="hi",
            html_body="<p>hi</p>",
            text_body="hi",
        )
