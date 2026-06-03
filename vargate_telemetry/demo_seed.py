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


def _usage_result(
    model: str,
    inp: int,
    out: int,
    cache_read: int = 0,
    *,
    cache_creation: int = 0,
    workspace_id: "str | None" = None,
) -> dict:
    return {
        "workspace_id": workspace_id,
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
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


_DEMO_BUDGET_NAME = "Demo: monthly spend cap"


def seed_budgets(tenant_id: str) -> dict[str, int]:
    """Seed a tenant-scoped monthly budget with Slack + email alert
    channels, plus one fired (unacknowledged) alert event — so the Budgets
    list/detail, the /alerts view, and the alert-channel edit / Send-test
    flows are all demoable. Idempotent (keyed on the budget name)."""
    from datetime import date

    recipients = {
        "email": ["alerts@demo.example.com"],
        # A placeholder webhook — the channel shows as configured ("Slack:
        # 1 webhook"); swap a real URL via the Edit form to see live
        # delivery. Send-test-alert will attempt it (best-effort).
        "slack_webhook": [
            "https://hooks.slack.com/services/T00DEMO00/B00DEMO00/"
            "demoDEMOdemoDEMOdemo0000"
        ],
        "pagerduty_key": [],
    }
    added = 0
    with session_scope(tenant_id) as s:
        existing = s.execute(
            sql_text(
                "SELECT id FROM budgets WHERE tenant_id = :t AND name = :n "
                "AND deleted_at IS NULL"
            ),
            {"t": tenant_id, "n": _DEMO_BUDGET_NAME},
        ).first()
        if existing is None:
            bid = s.execute(
                sql_text(
                    "INSERT INTO budgets (tenant_id, name, scope_kind, "
                    "scope_value, period, threshold_usd, alert_recipients) "
                    "VALUES (:t, :n, 'tenant', NULL, 'monthly', 500.00, "
                    "CAST(:r AS jsonb)) RETURNING id"
                ),
                {"t": tenant_id, "n": _DEMO_BUDGET_NAME, "r": json.dumps(recipients)},
            ).scalar_one()
            added += 1
        else:
            bid = existing.id

        has_event = (
            s.execute(
                sql_text(
                    "SELECT 1 FROM budget_alert_events WHERE budget_id = :b "
                    "LIMIT 1"
                ),
                {"b": str(bid)},
            ).first()
            is not None
        )
        if not has_event:
            s.execute(
                sql_text(
                    "INSERT INTO budget_alert_events (budget_id, tenant_id, "
                    "period_start, threshold_crossed, current_spend_usd) "
                    "VALUES (:b, :t, :ps, 0.85, 425.00)"
                ),
                {
                    "b": str(bid),
                    "t": tenant_id,
                    "ps": date.today().replace(day=1),
                },
            )
    return {"added": added, "skipped": 1 - added}


# ── volume seed (a realistic ~16-person org: many users + millions of
# tokens) for a full showcase. Separate from the minimal seed_all so it's
# opt-in (scripts/seed_demo.py --volume). Dates are relative to today so
# the Users dashboard's 7-day rollup + per-user spend look live; same-day
# re-runs dedup (absolute-date external_ids), later re-runs add fresh
# recent data. ──

_VOL_DOMAIN = "acme-demo.example"
_VOL_ROSTER = [
    ("Carol Diaz", "carol"), ("Dave Park", "dave"), ("Erin Walsh", "erin"),
    ("Frank Ito", "frank"), ("Grace Kim", "grace"), ("Heidi Roy", "heidi"),
    ("Ivan Petrov", "ivan"), ("Judy Chen", "judy"), ("Karl Mraz", "karl"),
    ("Liam OShea", "liam"), ("Mona Farah", "mona"), ("Nora Blum", "nora"),
    ("Omar Haddad", "omar"), ("Priya Rao", "priya"), ("Quinn Ross", "quinn"),
    ("Rosa Vela", "rosa"),
]
_VOL_MODELS = ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"]
_VOL_SURFACES = ["claude_code", "claude_web", "claude_desktop"]
_VOL_PII_SAMPLES = [
    "Customer wrote in from jane.roe@client.example, phone +1 (212) 555-0188.",
    "The failing run used key sk-ant-api01-DEMOnotreal0123456789abcdefgh and SSN 401-55-9210.",
    "Card on file 4012 8888 8888 1881 needs updating before renewal.",
    "Please summarize the Q2 board deck and flag anything sensitive.",
    "Draft a reply to the SOC 2 security questionnaire from acme.",
]


def _ensure_user(tenant_id: str, email: str, name: str, handle: str) -> int:
    """Create a member users row (non-loginable; fake sub) so the actor
    stitches into the Users dashboard via email match. Idempotent."""
    with engine.begin() as conn:
        if conn.execute(
            sql_text("SELECT 1 FROM users WHERE tenant_id = :t AND email = :e"),
            {"t": tenant_id, "e": email},
        ).first():
            return 0
        conn.execute(
            sql_text(
                "INSERT INTO users (id, email, sso_provider, sso_subject_id, "
                "name, tenant_id, role) VALUES (gen_random_uuid(), :e, "
                "'google', :sub, :n, :t, 'member')"
            ),
            {"e": email, "sub": f"demo-stitch-{handle}", "n": name, "t": tenant_id},
        )
    return 1


def _seed_volume_content(tenant_id: str, today, rng) -> int:
    from datetime import datetime, timedelta, timezone

    added = 0
    for idx, (name, handle) in enumerate(_VOL_ROSTER[:10]):  # 10 users w/ content
        email = f"{handle}@{_VOL_DOMAIN}"
        uid = f"demo-{handle}"
        chat_id = f"demo-vol-chat-{handle}"
        day = today - timedelta(days=idx % 6)
        for m in range(4):
            ext = f"demo:vol:content:{handle}:{m}"
            if _content_exists(tenant_id, ext):
                continue
            role = "user" if m % 2 == 0 else "assistant"
            text = (
                rng.choice(_VOL_PII_SAMPLES)
                if role == "user"
                else "Here is a summary of what you asked for."
            )
            ref, chash, size = store_content(tenant_id, text.encode("utf-8"))
            append_telemetry_record(
                tenant_id,
                record_type="chat_message",
                source_api=_SRC_CONTENT,
                external_id=ext,
                occurred_at=datetime(day.year, day.month, day.day, 10 + m, tzinfo=timezone.utc),
                content_hash=chash,
                content_ref=ref,
                content_size_bytes=size,
                subject_user_id=uid,
                record_metadata={
                    "chat_id": chat_id,
                    "message_id": ext,
                    "role": role,
                    "chat_name": f"{name.split()[0]}'s working session",
                    "model": "claude-opus-4-7",
                    "user_email": email,
                },
            )
            added += 1
    # A couple of deleted chats for tombstone variety.
    from vargate_telemetry import content_deletion

    for handle in ("carol", "dave"):
        content_deletion.delete_chat(
            tenant_id,
            f"demo-vol-chat-{handle}",
            reason="demo: data-subject erasure request",
            requested_by="seed_demo",
        )
    return added


def seed_volume(tenant_id: str, *, days: int = 30) -> dict[str, int]:
    """Seed a realistic-volume org: ~16 users with activity across `days`
    days (MCP tool calls priced per user, plus code-analytics / activity
    events) + org-level usage totalling millions of tokens + content for a
    subset. Idempotent per day (absolute-date external_ids)."""
    import random
    from datetime import date, datetime, timedelta, timezone

    if not tenant_id:
        raise ValueError("tenant_id required")
    ensure_tenant(tenant_id)
    rng = random.Random(42)
    today = date.today()
    users_added = events_added = usage_added = 0

    for name, handle in _VOL_ROSTER:
        email = f"{handle}@{_VOL_DOMAIN}"
        uid = f"demo-{handle}"
        users_added += _ensure_user(tenant_id, email, name, handle)
        for d in range(days):
            if rng.random() > 0.6:  # ~60% of days active
                continue
            day = today - timedelta(days=d)
            for i in range(rng.randint(1, 6)):
                src = rng.choices(
                    ["mcp", "code_analytics", "compliance_activities"],
                    weights=[6, 2, 2],
                )[0]
                model = rng.choice(_VOL_MODELS)
                surface = rng.choice(_VOL_SURFACES)
                inp, out = rng.randint(2000, 60000), rng.randint(500, 20000)
                occurred = datetime(
                    day.year, day.month, day.day,
                    rng.randint(8, 18), rng.randint(0, 59), tzinfo=timezone.utc,
                )
                ext = f"demo:vol:{src}:{handle}:{day.isoformat()}:{i}"
                if src == "mcp":
                    rtype = "tool_call"
                    md = {
                        "user_email": email,
                        "subject_user_id": uid,
                        "model": model,
                        "surface": surface,
                        "kind": "tool_use",
                        "input_tokens_estimate": inp,
                        "output_tokens_estimate": out,
                    }
                else:
                    rtype = "code_review" if src == "code_analytics" else "activity"
                    md = {
                        "actor": {"type": "user_actor", "email_address": email},
                        "surface": surface,
                        "model": model,
                        "summary": f"{rtype} by {name}",
                    }
                if _append_event(
                    tenant_id,
                    source_api=src,
                    record_type=rtype,
                    external_id=ext,
                    occurred_at=occurred,
                    metadata=md,
                    subject_user_id=uid,
                ):
                    events_added += 1

    # Org-level admin usage — priced per-model and engineered so the
    # Insights cards light up off real data:
    #   - model_mix: recent 7d is Opus, prior 7d is Sonnet (a cost-
    #     impactful week-over-week share shift),
    #   - cache_efficiency: the Opus workload writes far more cache than
    #     it reads back (a low hit ratio worth flagging),
    #   - workspace_attribution: spend split across a few workspaces,
    #   - cost_forecasting: the Opus-heavy recent week projects past the
    #     seeded monthly cap.
    # Deterministic (no rng) so the demo cards are stable night to night.
    _VOL_WS = [
        ("wrkspc_demo_eng", "Engineering", 0.6),
        ("wrkspc_demo_growth", "Growth", 0.3),
        ("wrkspc_demo_research", "Research", 0.1),
    ]
    with session_scope(tenant_id) as s:
        for wid, wname, _share in _VOL_WS:
            s.execute(
                sql_text(
                    "INSERT INTO workspaces (tenant_id, workspace_id, name) "
                    "VALUES (:t, :w, :n) "
                    "ON CONFLICT (tenant_id, workspace_id) DO NOTHING"
                ),
                {"t": tenant_id, "w": wid, "n": wname},
            )

    for d in range(days):
        day = today - timedelta(days=d)
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        if d < 7:
            model, base_in = "claude-opus-4-7", 3_000_000
        elif d < 14:
            model, base_in = "claude-sonnet-4-6", 1_200_000
        else:
            model, base_in = "claude-haiku-4-5", 800_000
        out = base_in // 6
        if model == "claude-opus-4-7":
            # poor reuse: writes a lot of cache, reads little back
            cache_creation, cache_read = int(base_in * 0.9), int(base_in * 0.07)
        else:
            cache_creation, cache_read = int(base_in * 0.05), int(base_in * 0.55)
        for wid, _wn, wshare in _VOL_WS:
            md = {
                "starting_at": start.isoformat().replace("+00:00", "Z"),
                "ending_at": (start + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                "results": [
                    _usage_result(
                        model,
                        int(base_in * wshare),
                        int(out * wshare),
                        cache_read=int(cache_read * wshare),
                        cache_creation=int(cache_creation * wshare),
                        workspace_id=wid,
                    )
                ],
            }
            ext = f"demo:vol:usage:{day.isoformat()}:{model}:{wid}"
            if _append_event(
                tenant_id,
                source_api="admin",
                record_type="usage",
                external_id=ext,
                occurred_at=start,
                metadata=md,
            ):
                usage_added += 1

    content_added = _seed_volume_content(tenant_id, today, rng)
    return {
        "users_added": users_added,
        "events_added": events_added,
        "usage_added": usage_added,
        "content_added": content_added,
    }


def seed_all(tenant_id: str) -> dict[str, dict[str, int]]:
    """Ensure the tenant + seed every surface. Idempotent."""
    if not tenant_id:
        raise ValueError("tenant_id required")
    ensure_tenant(tenant_id)
    return {
        "content": seed_content(tenant_id),
        "sessions": seed_sessions(tenant_id),
        "usage": seed_usage(tenant_id),
        "budgets": seed_budgets(tenant_id),
    }
