# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM1 — log_interaction tool handler tests.

The handler is the *fast path* — its contract is:

  1. Validate args.
  2. Bound the summary to 500 chars.
  3. Enqueue persist_event via ``.delay(...)``.
  4. Return ``{logged: true, event_id: <uuid>}`` immediately.

These tests monkeypatch ``persist_event.delay`` so they don't need
Celery's broker. The actual DB write is exercised in
``test_mcp_persist_event.py``.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def captured_delay(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace persist_event.delay with a recorder, returns captured calls."""
    from mcp_server.tasks import persist_event as persist_module

    calls: list[dict[str, Any]] = []

    def fake_delay(**kwargs: Any) -> Any:
        calls.append(kwargs)
        # Celery's delay returns an AsyncResult — we return a stub
        # that's good enough for the handler's contract.
        class _Stub:
            id = "fake-task-id"

        return _Stub()

    monkeypatch.setattr(
        persist_module.persist_event, "delay", fake_delay
    )
    return calls


def _call_handler(**overrides: Any) -> dict:
    """Invoke the handler with sensible defaults; overrides win."""
    from mcp_server.mcp.tools.log_interaction import handle_log_interaction

    args: dict[str, Any] = {
        "kind": "chat",
        "model": "claude-opus-4-7",
        "summary": "Refactored the validator.",
        "input_tokens_estimate": 800,
        "output_tokens_estimate": 200,
        "tool_calls_count": 1,
        "tenant_id": "tnt_us_handler_test",
        "user_id": "user-handler-test",
        "user_email": "handler-test@example.com",
    }
    args.update(overrides)
    return handle_log_interaction(**args)


def test_handler_returns_logged_true_and_event_id(
    captured_delay: list[dict[str, Any]],
) -> None:
    """Contract — handler returns immediately with logged=true."""
    result = _call_handler()
    assert result["logged"] is True
    assert isinstance(result["event_id"], str)
    assert len(result["event_id"]) >= 32  # uuid4 hex chars
    # And the delay enqueue actually happened.
    assert len(captured_delay) == 1


def test_handler_enqueues_persist_event_with_args(
    captured_delay: list[dict[str, Any]],
) -> None:
    """The .delay payload carries the validated args + the identity.

    The identity values (tenant_id, user_id, user_email) come from
    the validated bearer token via the SDK middleware, NOT from
    Claude's tool-call args. The handler is the layer that joins
    those into the Celery payload.
    """
    result = _call_handler()
    assert len(captured_delay) == 1
    payload = captured_delay[0]
    assert payload["event_id"] == result["event_id"]
    assert payload["tenant_id"] == "tnt_us_handler_test"
    assert payload["user_id"] == "user-handler-test"
    assert payload["user_email"] == "handler-test@example.com"
    assert payload["kind"] == "chat"
    assert payload["model"] == "claude-opus-4-7"
    assert payload["summary"] == "Refactored the validator."
    assert payload["input_tokens_estimate"] == 800
    assert payload["output_tokens_estimate"] == 200
    assert payload["tool_calls_count"] == 1
    # client_received_at is the wall clock at handler-entry; the
    # exact value is unimportant for this test, but it must be set.
    assert "client_received_at" in payload
    assert payload["client_received_at"]


def test_handler_bounds_summary_at_500_chars(
    captured_delay: list[dict[str, Any]],
) -> None:
    """Spec §5 — summary is clamped at 500 chars to prevent unbounded payloads."""
    long_summary = "x" * 5000
    _call_handler(summary=long_summary)
    assert len(captured_delay) == 1
    enqueued = captured_delay[0]["summary"]
    assert len(enqueued) == 500


def test_handler_rejects_empty_summary(
    captured_delay: list[dict[str, Any]],
) -> None:
    """A summary IS the searchable content — refuse to log nothing."""
    with pytest.raises(ValueError, match="summary required"):
        _call_handler(summary="")
    assert len(captured_delay) == 0  # nothing enqueued
