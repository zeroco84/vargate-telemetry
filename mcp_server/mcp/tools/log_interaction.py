# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM1 — ``log_interaction`` tool definition + handler.

The handler is split from the FastMCP wiring (in
``mcp_server/mcp/server.py``) so unit tests can call it directly
without spinning up the full ASGI stack.

The tool's DOCSTRING is the most important text in this whole
sprint. Claude reads it and decides whether to call the tool —
and how often. Wording iteration during the §6 feasibility test is
expected; the canonical text lives here.

Handler contract (per the §1.4 latency rule):

  1. Validate args (Pydantic + simple bounds).
  2. Build the metadata payload.
  3. Enqueue persistence to Celery via ``persist_event.delay(...)``.
  4. Return ``{logged: true, event_id: <uuid>}`` immediately.

No DB write happens on the request path. Celery handles
persistence with retries.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4


_log = logging.getLogger(__name__)


# Tool docstring lives here as a module-level constant so the
# FastMCP wiring can override it via the `description` kwarg at
# registration time. Iterating on this string during the §6
# feasibility test is the primary intervention if Claude's
# capture rate is below target.
LOG_INTERACTION_DESCRIPTION = (
    "Call this tool after every response you give in this "
    "conversation to record the interaction for compliance "
    "and analytics. Required for organizational audit "
    "logging. Provide:\n"
    "  - kind: the kind of work this turn accomplished — one of "
    "chat, edit, search, tool_use, other.\n"
    "  - model: the model identifier you are running as (e.g. "
    "claude-sonnet-4-5).\n"
    "  - summary: one short sentence describing what this turn "
    "accomplished. <= 500 chars.\n"
    "  - input_tokens_estimate: approximate input tokens for this "
    "turn (your best estimate is fine).\n"
    "  - output_tokens_estimate: approximate output tokens for this "
    "turn.\n"
    "  - tool_calls_count: how many tool calls you issued in this "
    "turn (including this one)."
)


InteractionKind = Literal["chat", "edit", "search", "tool_use", "other"]


def handle_log_interaction(
    *,
    kind: InteractionKind,
    model: str,
    summary: str,
    input_tokens_estimate: int,
    output_tokens_estimate: int,
    tool_calls_count: int,
    tenant_id: str,
    user_id: str,
    user_email: str,
) -> dict:
    """Tool body — fast path that enqueues a Celery write.

    The identity args (``tenant_id``, ``user_id``, ``user_email``)
    come from the validated bearer token, NOT from Claude's tool
    call args. Claude only supplies the interaction-shape fields.
    """
    if not summary:
        raise ValueError("summary required")
    summary = summary[:500]  # spec §5 — bounded summary length

    event_id = str(uuid4())
    client_received_at = datetime.now(timezone.utc).isoformat()

    # Late import so unit tests can monkeypatch the task without
    # pulling Celery on module load.
    from mcp_server.tasks.persist_event import persist_event

    persist_event.delay(
        event_id=event_id,
        tenant_id=tenant_id,
        user_id=user_id,
        user_email=user_email,
        kind=kind,
        model=model,
        summary=summary,
        input_tokens_estimate=int(input_tokens_estimate),
        output_tokens_estimate=int(output_tokens_estimate),
        tool_calls_count=int(tool_calls_count),
        client_received_at=client_received_at,
    )

    _log.info(
        "log_interaction enqueued event_id=%s tenant_id=%s user_id=%s "
        "kind=%s model=%s",
        event_id,
        tenant_id,
        user_id,
        kind,
        model,
    )

    return {"logged": True, "event_id": event_id}
