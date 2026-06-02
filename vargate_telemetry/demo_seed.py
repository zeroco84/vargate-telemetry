# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Demo-data seeding (TM6 T6.S) — importable logic behind
``scripts/seed_demo.py``.

Populates the three dashboards on a fresh build / for a customer
walkthrough, exercising the REAL pipeline (chain-bound
``append_telemetry_record`` + AES-GCM ``store_content`` blobs) so the
seeded data verifies + decrypts + redacts + deletes exactly like
production data:

  - **Content** (compliance_content): chats incl. ones with PII (to demo
    redaction / reveal) and one that's deleted (tombstone).
  - **Sessions** (code_analytics + compliance_activities): events across
    a couple of actors + days, with the ``metadata.actor`` envelope the
    Sessions grouping reads.
  - **Usage** (admin): a few days of token-usage breakdown rows.

Idempotent: every record has a deterministic ``demo:`` external_id, so
re-running skips what already exists (dedup-before-store for content →
no orphan blobs; IntegrityError-skip for blob-less events). NEVER deletes
chain records (append-only); re-seeding only adds what's missing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

from vargate_telemetry.chain import append_telemetry_record
from vargate_telemetry.db import engine, session_scope
from vargate_telemetry.storage.content import store_content

_SRC_CONTENT = "compliance_content"


def _md_hash(metadata: dict[str, Any]) -> bytes:
    """content_hash for a blob-less event: SHA-256 of its canonical
    metadata (meaningful + unique — same idea as the lifecycle events)."""
    canonical = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def _at(day: int, hour: int = 9) -> datetime:
    """Fixed base date so re-runs + occurred_at are deterministic."""
    return datetime(2026, 5, 19 + day, hour, 0, 0, tzinfo=timezone.utc)


def ensure_tenant(tenant_id: str) -> None:
    """Create the tenant row (if missing), provision its DEK, and seal a
    placeholder Compliance Access Key so the demo tenant is on the
    compliance tier — the content view, export, and deletion all gate on
    that capability. All idempotent."""
    from vargate_telemetry.anthropic import ANTHROPIC_COMPLIANCE_KEY_SECRET
    from vargate_telemetry.crypto.seal import provision_tenant_dek, seal_secret

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "billing_status) VALUES (:t, 'us', true, 'paying') "
                "ON CONFLICT (tenant_id) DO NOTHING"
            ),
            {"t": tenant_id},
        )
    provision_tenant_dek(tenant_id)
    seal_secret(
        tenant_id,
        ANTHROPIC_COMPLIANCE_KEY_SECRET,
        b"sk-ant-api01-demo-compliance-key",
    )


def _content_exists(tenant_id: str, external_id: str) -> bool:
    with session_scope(tenant_id) as s:
        return (
            s.execute(
                sql_text(
                    "SELECT 1 FROM telemetry_records WHERE tenant_id = :t "
                    "AND source_api = :s AND external_id = :e LIMIT 1"
                ),
                {"t": tenant_id, "s": _SRC_CONTENT, "e": external_id},
            ).first()
            is not None
        )


def _append_event(
    tenant_id: str,
    *,
    source_api: str,
    record_type: str,
    external_id: str,
    occurred_at: datetime,
    metadata: dict[str, Any],
    subject_user_id: Optional[str] = None,
) -> bool:
    """Append a blob-less event; returns False if it already exists."""
    try:
        append_telemetry_record(
            tenant_id,
            record_type=record_type,
            source_api=source_api,
            external_id=external_id,
            occurred_at=occurred_at,
            content_hash=_md_hash(metadata),
            record_metadata=metadata,
            subject_user_id=subject_user_id,
        )
        return True
    except IntegrityError:
        return False


# ── content (TM6 demo: redaction + deletion surfaces) ──────────────────────

_CHATS = [
    {
        "id": "demo-onboarding",
        "name": "Onboarding questions",
        "user_id": "demo-user-alice",
        "email": "alice@demo.example.com",
        "day": 1,
        "messages": [
            ("user", "Hi! You can reach me at alice@demo.example.com or +1 (415) 555-0142."),
            ("assistant", "Thanks — I've noted your contact details. How can I help you onboard?"),
            ("user", "Walk me through connecting our first data source."),
        ],
    },
    {
        "id": "demo-incident",
        "name": "Incident triage",
        "user_id": "demo-user-bob",
        "email": "bob@demo.example.com",
        "day": 1,
        "messages": [
            ("user", "The job failed using key sk-ant-api01-DEMOkeydonotuse0123456789abcdef and SSN 123-45-6789 leaked to logs."),
            ("assistant", "I've flagged the exposed key + SSN. Rotate the key and scrub the logs."),
        ],
    },
    {
        "id": "demo-old-request",
        "name": "Old data request",
        "user_id": "demo-user-alice",
        "email": "alice@demo.example.com",
        "day": 2,
        "messages": [
            ("user", "Please summarize last quarter's numbers."),
            ("assistant", "Here is the summary you asked for."),
        ],
    },
]

# The chat that gets deleted to demo the tombstone / chain-safe deletion.
_DEMO_DELETED_CHAT = "demo-old-request"


def seed_content(tenant_id: str) -> dict[str, int]:
    added = skipped = 0
    for chat in _CHATS:
        for idx, (role, text) in enumerate(chat["messages"]):
            ext = f"demo:{chat['id']}:msg{idx}"
            if _content_exists(tenant_id, ext):
                skipped += 1
                continue
            ref, chash, size = store_content(tenant_id, text.encode("utf-8"))
            append_telemetry_record(
                tenant_id,
                record_type="chat_message",
                source_api=_SRC_CONTENT,
                external_id=ext,
                occurred_at=_at(chat["day"], 9 + idx),
                content_hash=chash,
                content_ref=ref,
                content_size_bytes=size,
                subject_user_id=chat["user_id"],
                record_metadata={
                    "chat_id": chat["id"],
                    "message_id": ext,
                    "role": role,
                    "chat_name": chat["name"],
                    "model": "claude-opus-4-7",
                    "user_email": chat["email"],
                },
            )
            added += 1

    # Demo the deletion tombstone (idempotent — re-runs report already_deleted).
    from vargate_telemetry import content_deletion

    content_deletion.delete_chat(
        tenant_id,
        _DEMO_DELETED_CHAT,
        reason="demo: data-subject deletion request",
        requested_by="seed_demo",
    )
    return {"added": added, "skipped": skipped}


# ── sessions (code_analytics + compliance_activities) ──────────────────────

_SESSION_EVENTS = [
    ("code_analytics", "code_review", "demo-user-alice", "alice@demo.example.com", 1, "claude_code"),
    ("code_analytics", "code_review", "demo-user-alice", "alice@demo.example.com", 1, "claude_code"),
    ("compliance_activities", "activity", "demo-user-alice", "alice@demo.example.com", 2, "claude_web"),
    ("code_analytics", "code_review", "demo-user-bob", "bob@demo.example.com", 1, "claude_code"),
    ("compliance_activities", "activity", "demo-user-bob", "bob@demo.example.com", 2, "claude_web"),
    ("compliance_activities", "activity", "demo-user-bob", "bob@demo.example.com", 2, "claude_web"),
]


def seed_sessions(tenant_id: str) -> dict[str, int]:
    added = skipped = 0
    for i, (src, rtype, uid, email, day, surface) in enumerate(_SESSION_EVENTS):
        ext = f"demo:session:{src}:{uid}:{i}"
        md = {
            "actor": {"type": "user_actor", "email_address": email},
            "surface": surface,
            "summary": f"demo {rtype} event for {email}",
        }
        if _append_event(
            tenant_id,
            source_api=src,
            record_type=rtype,
            external_id=ext,
            occurred_at=_at(day, 10 + i),
            metadata=md,
            subject_user_id=uid,
        ):
            added += 1
        else:
            skipped += 1
    return {"added": added, "skipped": skipped}


# ── usage (admin token-usage breakdowns) ───────────────────────────────────


def _usage_result(model: str, inp: int, out: int, cache_read: int = 0) -> dict:
    return {
        "workspace_id": None,
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": 0,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 0,
            "ephemeral_1h_input_tokens": 0,
        },
    }


def seed_usage(tenant_id: str) -> dict[str, int]:
    added = skipped = 0
    days = [
        ("claude-opus-4-7", 12000, 3400, 8000),
        ("claude-opus-4-7", 15500, 4100, 9000),
        ("claude-sonnet-4-5", 42000, 9800, 30000),
    ]
    for d, (model, inp, out, cache_read) in enumerate(days, start=1):
        start = _at(d, 0)
        end = _at(d + 1, 0)
        md = {
            "starting_at": start.isoformat().replace("+00:00", "Z"),
            "ending_at": end.isoformat().replace("+00:00", "Z"),
            "results": [_usage_result(model, inp, out, cache_read)],
        }
        ext = f"demo:usage:{start.isoformat()}:{end.isoformat()}:{model}:-"
        if _append_event(
            tenant_id,
            source_api="admin",
            record_type="usage",
            external_id=ext,
            occurred_at=start,
            metadata=md,
        ):
            added += 1
        else:
            skipped += 1
    return {"added": added, "skipped": skipped}


def seed_all(tenant_id: str) -> dict[str, dict[str, int]]:
    """Ensure the tenant + seed all three surfaces. Idempotent."""
    if not tenant_id:
        raise ValueError("tenant_id required")
    ensure_tenant(tenant_id)
    return {
        "content": seed_content(tenant_id),
        "sessions": seed_sessions(tenant_id),
        "usage": seed_usage(tenant_id),
    }
