# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM1 — Celery task that persists an MCP interaction.

Called by the ``log_interaction`` tool handler via ``.delay(...)``.
The tool handler returns to Claude in <500ms; this task runs out-
of-band on a worker, reuses the existing hash-chain insert primitive
(:func:`vargate_telemetry.chain.append_telemetry_record`) so the
new row gets a valid ``chain_seq`` / ``chain_prev_hash`` / ``chain_self_hash``.

Idempotency: the row's ``external_id`` is
``mcp:{tenant_id}:{user_id}:{event_id}``. The
``(tenant_id, source_api, external_id)`` UNIQUE constraint on
``telemetry_records`` (T2.x) catches re-deliveries from the Celery
broker — we treat ``IntegrityError`` as a successful no-op rather
than a retry.

The Celery default retry policy applies for genuine errors (DB
down, etc.).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from vargate_telemetry.celery_app import celery_app
from vargate_telemetry.chain import append_telemetry_record


_log = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    max_retries=3,
    name="mcp_server.tasks.persist_event.persist_event",
)
def persist_event(
    self,
    *,
    event_id: str,
    tenant_id: str,
    user_id: str,
    user_email: str,
    kind: str,
    model: str,
    summary: str,
    input_tokens_estimate: int,
    output_tokens_estimate: int,
    tool_calls_count: int,
    surface: str | None = None,
    client_received_at: str,
) -> dict[str, Any]:
    """Write one MCP interaction to telemetry_records.

    All args are JSON-serializable (Celery's default serializer).
    ``client_received_at`` is the ISO-8601 UTC timestamp captured at
    handler-entry-time so out-of-order Celery dispatch doesn't
    re-shuffle the timeline.

    Returns a small dict for the Celery result backend — useful in
    tests and for ad-hoc debugging via ``celery inspect``.
    """
    if not tenant_id:
        raise ValueError("tenant_id required")
    occurred_at = datetime.fromisoformat(client_received_at)

    metadata = {
        "kind": kind,
        "model": model,
        "summary": summary,
        "input_tokens_estimate": int(input_tokens_estimate),
        "output_tokens_estimate": int(output_tokens_estimate),
        "tool_calls_count": int(tool_calls_count),
        # TM4 #3 — Claude-self-reported client surface (claude_code /
        # claude_desktop / claude_web / other). None when the client
        # predates the field or didn't report it; the read-path falls
        # back to the `kind` heuristic in that case.
        "surface": surface,
        "user_email": user_email,
        "subject_user_id": user_id,
        "event_id": event_id,
    }

    # Content hash: SHA-256 of the canonical metadata blob. We don't
    # store a separate content blob for MCP records — the metadata
    # IS the content — but the chain layer requires a 32-byte hash
    # regardless. Hash the canonical-JSON form so the chain proof
    # has a tamper-detect target on the metadata payload.
    canonical = json.dumps(
        metadata, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    content_hash = hashlib.sha256(canonical).digest()

    external_id = f"mcp:{tenant_id}:{user_id}:{event_id}"

    try:
        record = append_telemetry_record(
            tenant_id,
            record_type="mcp_interaction",
            source_api="mcp",
            external_id=external_id,
            occurred_at=occurred_at,
            content_hash=content_hash,
            record_metadata=metadata,
            subject_user_id=user_id,
        )
    except IntegrityError:
        # Idempotent: re-delivery from the broker, or a parallel
        # tool call that already won the dedup race. Log + move on.
        _log.info(
            "persist_event: dedup on external_id=%s — already persisted",
            external_id,
        )
        return {"persisted": False, "reason": "dedup"}
    except Exception as exc:  # pragma: no cover — retry path
        _log.exception("persist_event failed for %s", external_id)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)

    return {
        "persisted": True,
        "event_id": event_id,
        "record_id": str(record.id),
        "chain_seq": record.chain_seq,
    }
