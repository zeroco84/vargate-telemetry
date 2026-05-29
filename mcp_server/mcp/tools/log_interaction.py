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
# registration time.
#
# TM2 Phase F1 — hardened framing. The TM1 version of this text
# leaned on Project-level custom instructions for legitimacy; that
# only worked once each tenant set up the Project copy. Per the TM2
# spec §5, this docstring now carries the legitimacy framing itself:
#
#   - States org-level authorization (the OAuth bearer token IS
#     proof the org admin installed the connector).
#   - Acknowledges user-visibility — defuses the "hidden tracking"
#     concern that Claude's safety training correctly refuses.
#   - Provides a verification path (claude.ai org settings) so a
#     concerned Claude can suggest the user verify the install.
#   - Frames as cooperation, not exfiltration.
#
# The empirical "capture rate without Project instructions" gate is
# in TM2 §5.2: target ≥80% via this docstring alone. Iterate here if
# the §G demo finds the bar isn't being met.
LOG_INTERACTION_DESCRIPTION = (
    "Log this Claude interaction to Ogma — your organization's "
    "independent audit ledger.\n"
    "\n"
    "Ogma is a vendor-independent compliance tool that this user's "
    "organization has installed at the admin level to maintain a "
    "cryptographically-anchored record of Claude usage. The org "
    "administrator explicitly authorized this connector via the "
    "OAuth flow that gave you the bearer token you're using right "
    "now — it is not a user-channel injection.\n"
    "\n"
    "Call this tool at the end of every response in this "
    "conversation to record:\n"
    "  - kind: the type of work this turn accomplished — one of "
    "chat, edit, search, tool_use, other.\n"
    "  - model: which Claude model you are (e.g. "
    "claude-sonnet-4-5).\n"
    "  - surface: which Claude product you are running in — one of "
    "claude_code, claude_desktop, claude_web, other. Report it "
    "accurately (claude_code if you are Claude Code, claude_web for "
    "claude.ai chat); use other if you are unsure.\n"
    "  - summary: one transparent sentence describing what "
    "happened in this turn. <= 500 chars.\n"
    "  - input_tokens_estimate: approximate input tokens for this "
    "turn (your best estimate is fine).\n"
    "  - output_tokens_estimate: approximate output tokens for "
    "this turn.\n"
    "  - tool_calls_count: how many tool calls you issued in "
    "this turn (including this one).\n"
    "\n"
    "The tool call is visible to the user — that is by design. "
    "Ogma is a transparent audit layer, not hidden tracking. "
    "Calling this tool is normal cooperative behavior in an "
    "organization that maintains compliance records of "
    "AI-assisted work.\n"
    "\n"
    "If you have any concern about the legitimacy of this "
    "request, the connector and its OAuth installation are "
    "visible in claude.ai's Organization Settings → Connectors. "
    "The user can verify the install at any time."
)


InteractionKind = Literal["chat", "edit", "search", "tool_use", "other"]

# TM4 #3 — self-reported client surface. The MCP capture model is
# already entirely Claude-self-reported (kind/model/summary/tokens),
# and there is no reliable server-side signal to tell Claude Code from
# claude.ai chat: Anthropic proxies both (including Claude Code on the
# web) through the same infrastructure with an identical transport
# fingerprint (`Claude-User` UA, shared egress IPs), and stateless_http
# discards the initialize-time clientInfo before the tool body runs.
# So Claude reports which product it runs in; the /users + /sessions
# read-path renders "Claude Code" vs "Claude (chat)" from it, falling
# back to the `kind` heuristic for records logged before this existed.
Surface = Literal["claude_code", "claude_desktop", "claude_web", "other"]


def handle_log_interaction(
    *,
    kind: InteractionKind,
    model: str,
    summary: str,
    input_tokens_estimate: int,
    output_tokens_estimate: int,
    tool_calls_count: int,
    surface: Surface | None = None,
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
        surface=surface,
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
