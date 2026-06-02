# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""PII detection + redaction (TM6 T6.3) — regex-first, stdlib only.

Detects common PII / secret patterns in captured content and masks them.
Used by the content view (mask by default; a privileged, audit-logged
reveal returns the original) and by the eDiscovery export (redacted by
default; full content is an explicit, logged option).

**Regex-first, by design.** This is a deterministic, dependency-free
first pass (the TM6 scope explicitly defers ML/NER detection). It errs
toward OVER-redaction (a false positive masks a non-secret; a false
negative leaks PII — over-redaction is the safer failure). The detector
is intentionally easy to extend: add a ``(label, pattern)`` to
``_PATTERNS``.

``detect_and_redact(text)`` returns ``(redacted_text, findings)`` where
findings is a list of ``{"type": <label>, "count": <n>}`` — counts only,
never the matched values (so a redaction summary can be shown/logged
without re-leaking the PII).
"""

from __future__ import annotations

import re
from typing import Any

# Ordered most-specific → loosest. Earlier patterns redact first, so their
# matches are gone before a looser pattern (e.g. phone) can re-match the
# same digits. The placeholder text never matches any pattern, so
# sequential substitution is safe.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Provider / cloud secrets (specific prefixes — low false-positive).
    (
        "api_key",
        re.compile(
            r"\b(?:sk-ant-[A-Za-z0-9_-]{16,}"
            r"|sk-[A-Za-z0-9]{20,}"
            r"|AKIA[0-9A-Z]{16}"
            r"|ghp_[A-Za-z0-9]{20,})\b"
        ),
    ),
    # US SSN (NNN-NN-NNNN).
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Credit-card-ish: 13–16 digits in 4-digit groups (optional - or space).
    ("credit_card", re.compile(r"\b(?:\d{4}[ -]?){3}\d{1,4}\b")),
    # Email.
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
    # IPv4.
    ("ip_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    # Phone (US-ish 10-digit, optional country code + separators). Loosest
    # — runs last so card/SSN/IP claim their digits first.
    (
        "phone",
        re.compile(
            r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
        ),
    ),
]


def _placeholder(label: str) -> str:
    return f"[redacted:{label}]"


def detect_and_redact(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(redacted_text, findings)``.

    ``findings`` is ``[{"type": label, "count": n}, ...]`` for each PII
    type that matched (counts only — never the matched values). When
    nothing matches, returns the input unchanged + an empty list.
    """
    if not text:
        return text, []

    counts: dict[str, int] = {}
    redacted = text
    for label, pattern in _PATTERNS:
        replacement = _placeholder(label)
        redacted, n = pattern.subn(replacement, redacted)
        if n:
            counts[label] = counts.get(label, 0) + n

    findings = [{"type": label, "count": counts[label]} for label, _ in _PATTERNS if label in counts]
    return redacted, findings


def redaction_findings(text: str) -> list[dict[str, Any]]:
    """The findings only (counts per type), without building the redacted
    string — for callers that return original content but still want to
    report what PII it contains."""
    _, findings = detect_and_redact(text)
    return findings
