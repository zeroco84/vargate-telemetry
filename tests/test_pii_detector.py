# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the regex-first PII detector (TM6 T6.3). Pure functions —
no DB / MinIO."""

from __future__ import annotations

from vargate_telemetry.pii_detector import detect_and_redact, redaction_findings


def _types(findings: list[dict]) -> set[str]:
    return {f["type"] for f in findings}


def test_email_is_redacted() -> None:
    red, f = detect_and_redact("contact jane.doe@example.com please")
    assert "jane.doe@example.com" not in red
    assert "[redacted:email]" in red
    assert {"type": "email", "count": 1} in f


def test_ssn_is_redacted() -> None:
    red, f = detect_and_redact("SSN 123-45-6789 on file")
    assert "123-45-6789" not in red
    assert "ssn" in _types(f)


def test_credit_card_is_redacted() -> None:
    red, f = detect_and_redact("card 4111 1111 1111 1111 exp")
    assert "4111" not in red
    assert "credit_card" in _types(f)


def test_api_key_is_redacted() -> None:
    red, f = detect_and_redact(
        "key sk-ant-api01-abcdefghij0123456789ABCDEFxyz here"
    )
    assert "sk-ant-api01" not in red
    assert "api_key" in _types(f)


def test_phone_is_redacted() -> None:
    red, f = detect_and_redact("call +1 (415) 555-1234 now")
    assert "555-1234" not in red
    assert "phone" in _types(f)


def test_ip_is_redacted() -> None:
    red, f = detect_and_redact("request from 192.168.1.42 logged")
    assert "192.168.1.42" not in red
    assert "ip_address" in _types(f)


def test_no_pii_passes_through_unchanged() -> None:
    text = "Draft the requirements for the launch plan."
    red, f = detect_and_redact(text)
    assert red == text
    assert f == []


def test_counts_multiple_of_same_type() -> None:
    _, f = detect_and_redact("mail a@b.com and c@d.org")
    counts = {x["type"]: x["count"] for x in f}
    assert counts["email"] == 2


def test_empty_string() -> None:
    assert detect_and_redact("") == ("", [])


def test_card_not_double_counted_as_phone() -> None:
    # A 16-digit card must redact as credit_card and NOT also as phone
    # (the more specific pattern claims the digits first).
    _, f = detect_and_redact("4111 1111 1111 1111")
    types = _types(f)
    assert "credit_card" in types
    assert "phone" not in types


def test_findings_only_helper() -> None:
    assert redaction_findings("x@y.com") == [{"type": "email", "count": 1}]
    assert redaction_findings("nothing here") == []
