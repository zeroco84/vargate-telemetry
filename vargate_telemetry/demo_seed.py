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

# OpenAI source_api values — kept in lockstep with the pull tasks'
# SOURCE_API_OPENAI_* constants (tasks/pull_openai_usage.py /
# pull_openai_costs.py / pull_openai_audit.py). Defined locally rather
# than imported so demo_seed stays free of the Celery task import graph
# (same posture as _SRC_CONTENT). The OpenAI seed lives further down
# (seed_openai_volume).
SOURCE_API_OPENAI_USAGE = "openai_admin_usage"
SOURCE_API_OPENAI_COSTS = "openai_admin_costs"
SOURCE_API_OPENAI_AUDIT = "openai_audit_logs"


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

    # Cross-vendor: layer OpenAI activity (projects/keys/users side tables
    # + per-(day, user) usage + authoritative per-project costs + a few
    # audit logs) onto the SAME roster so the API Usage vendor filter,
    # the cross-vendor Insights cards, and the Users list+detail render
    # OpenAI alongside Anthropic. Relative-dated → the nightly cron keeps
    # it inside the 7d/30d windows. Idempotent (demo: external_ids).
    openai_counts = seed_openai_volume(tenant_id, days=days)

    return {
        "users_added": users_added,
        "events_added": events_added,
        "usage_added": usage_added,
        "content_added": content_added,
        # OpenAI stream counts (added rows) so the --volume summary can
        # report both vendors. Keys mirror seed_openai_volume's return.
        "openai_projects_added": openai_counts["projects_added"],
        "openai_api_keys_added": openai_counts["api_keys_added"],
        "openai_users_added": openai_counts["users_added"],
        "openai_usage_added": openai_counts["usage_added"],
        "openai_costs_added": openai_counts["costs_added"],
        "openai_audit_added": openai_counts["audit_added"],
    }


# ── OpenAI cross-vendor volume (TM8 Phase F) ───────────────────────────────
#
# Seeds the same shapes the OpenAI pull tasks write (tasks/pull_openai_*.py)
# via the SAME real pipeline (append_telemetry_record for telemetry_records
# + raw ON CONFLICT upserts for the openai_projects / openai_api_keys /
# openai_users side tables). The seeded openai_users emails reuse the
# _VOL_ROSTER emails so OpenAI activity stitches into the SAME users rows
# as Claude (the alias reconciler email-matches metadata.user_email →
# users.email; it runs lazily on GET /api/users).
#
# Source-of-truth shapes are pinned to:
#   - pull_openai_usage._normalize_result  (usage metadata + external_id)
#   - pull_openai_costs._normalize_cost_result  (costs metadata + external_id)
#   - pull_openai_audit._normalize_audit  (audit metadata + external_id)
#   - openai/types.py  (UsageCompletionsResult / CostResult / AuditLogEntry
#     model_dump shapes — what lands under metadata['result'] / 'entry')
#   - pricing/vendor_cost._estimate_openai_usage / insights/spend_data /
#     api/usage._openai_branch  (which read result.input_uncached_tokens /
#     input_cached_tokens / output_tokens / model / project_id / api_key_id)
#
# Two OpenAI projects + three API keys. One model mix per day (gpt-4o +
# gpt-4o-mini) so model_mix's recent-7d-vs-prior-7d cross-vendor share is
# stable; volume comparable to the Anthropic demo usage.

_OPENAI_PROJECTS = [
    ("proj_demo_platform", "Platform"),
    ("proj_demo_apps", "Applications"),
]
# (api_key_id, project_id, display name)
_OPENAI_API_KEYS = [
    ("key_demo_backend", "proj_demo_platform", "backend-service"),
    ("key_demo_batch", "proj_demo_platform", "nightly-batch"),
    ("key_demo_webapp", "proj_demo_apps", "webapp-frontend"),
]
# The two demo models + their per-row token shape (uncached, cached,
# output, requests). gpt-4o is the heavier model; gpt-4o-mini the cheap
# high-volume one — same split posture as a real org.
_OPENAI_MODELS = [
    # model, input_uncached, input_cached, output, num_requests
    ("gpt-4o-2024-08-06", 9000, 3000, 2600, 14),
    ("gpt-4o-mini-2024-07-18", 26000, 8000, 7400, 41),
]

# An OpenAI user id whose email is deliberately NOT in the roster — its
# usage lands in the Unmapped panel (the cross-vendor analogue of an
# Anthropic api-key actor with no email). Seeded into openai_users with a
# null email so it resolves to no user_email and never auto-matches.
_OPENAI_UNMAPPED_USER_ID = "user-demo-svcacct"


def _openai_user_id(handle: str) -> str:
    """Deterministic OpenAI user id for a roster handle (opaque, like the
    vendor's ``user-XXXX``)."""
    return f"user-demo-{handle}"


def _seed_openai_side_tables(tenant_id: str) -> dict[str, int]:
    """Upsert the openai_projects / openai_api_keys / openai_users side
    tables (migration 0025), mirroring pull_openai_projects' UPSERTs.

    openai_users gets one row per roster user (email = the roster email,
    so attribution stitches) PLUS exactly one unmapped identity (null
    email). Idempotent — ON CONFLICT DO UPDATE, returns the count of rows
    touched (inserts only, for the summary)."""
    from datetime import datetime, timezone

    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    projects_added = api_keys_added = users_added = 0

    with session_scope(tenant_id) as s:
        for project_id, name in _OPENAI_PROJECTS:
            res = s.execute(
                sql_text(
                    """
                    INSERT INTO openai_projects
                        (tenant_id, project_id, name, status,
                         created_at_openai)
                    VALUES (:t, :p, :n, 'active', :c)
                    ON CONFLICT (tenant_id, project_id)
                    DO UPDATE SET
                        name = EXCLUDED.name,
                        status = EXCLUDED.status,
                        synced_at = now()
                    RETURNING (xmax = 0) AS was_insert
                    """
                ),
                {"t": tenant_id, "p": project_id, "n": name, "c": created},
            ).first()
            if res is not None and res.was_insert:
                projects_added += 1

        for api_key_id, project_id, name in _OPENAI_API_KEYS:
            res = s.execute(
                sql_text(
                    """
                    INSERT INTO openai_api_keys
                        (tenant_id, api_key_id, project_id, name,
                         created_at_openai, last_used_at)
                    VALUES (:t, :k, :p, :n, :c, :c)
                    ON CONFLICT (tenant_id, api_key_id)
                    DO UPDATE SET
                        project_id = EXCLUDED.project_id,
                        name = EXCLUDED.name,
                        synced_at = now()
                    RETURNING (xmax = 0) AS was_insert
                    """
                ),
                {
                    "t": tenant_id,
                    "k": api_key_id,
                    "p": project_id,
                    "n": name,
                    "c": created,
                },
            ).first()
            if res is not None and res.was_insert:
                api_keys_added += 1

        # One openai_users row per roster user (email match → stitch).
        for name, handle in _VOL_ROSTER:
            email = f"{handle}@{_VOL_DOMAIN}"
            res = s.execute(
                sql_text(
                    """
                    INSERT INTO openai_users
                        (tenant_id, openai_user_id, email, name, role)
                    VALUES (:t, :u, :e, :n, 'member')
                    ON CONFLICT (tenant_id, openai_user_id)
                    DO UPDATE SET
                        email = EXCLUDED.email,
                        name = EXCLUDED.name,
                        role = EXCLUDED.role,
                        synced_at = now()
                    RETURNING (xmax = 0) AS was_insert
                    """
                ),
                {
                    "t": tenant_id,
                    "u": _openai_user_id(handle),
                    "e": email,
                    "n": name,
                },
            ).first()
            if res is not None and res.was_insert:
                users_added += 1

        # The unmapped identity: an OpenAI user with NO email (a service
        # account). Its usage rows resolve to no user_email and land in
        # the Users "unmapped activity" panel.
        res = s.execute(
            sql_text(
                """
                INSERT INTO openai_users
                    (tenant_id, openai_user_id, email, name, role)
                VALUES (:t, :u, NULL, 'Batch Service Account',
                        'service_account')
                ON CONFLICT (tenant_id, openai_user_id)
                DO UPDATE SET name = EXCLUDED.name, synced_at = now()
                RETURNING (xmax = 0) AS was_insert
                """
            ),
            {"t": tenant_id, "u": _OPENAI_UNMAPPED_USER_ID},
        ).first()
        if res is not None and res.was_insert:
            users_added += 1

    return {
        "projects_added": projects_added,
        "api_keys_added": api_keys_added,
        "users_added": users_added,
    }


def _openai_usage_result_dict(
    *,
    model: str,
    user_id: str,
    api_key_id: str,
    project_id: str,
    input_uncached: int,
    input_cached: int,
    output: int,
    num_requests: int,
) -> dict[str, Any]:
    """Build one grouped usage result row matching
    ``UsageCompletionsResult.model_dump(mode="json")`` (openai/types.py).

    ``input_tokens`` is the TOTAL (uncached + cached) — the double-count
    trap the cost path must NOT bill directly; the text sub-splits mirror
    the recon §2 shape so the stored metadata is realistic. This dict
    lands verbatim under ``metadata['result']``; api/usage + spend_data +
    vendor_cost all read ``input_uncached_tokens`` / ``input_cached_tokens``
    / ``output_tokens`` / ``model`` / ``project_id`` / ``api_key_id`` from
    it."""
    return {
        "object": "organization.usage.completions.result",
        "project_id": project_id,
        "user_id": user_id,
        "api_key_id": api_key_id,
        "model": model,
        "batch": None,
        "service_tier": None,
        "num_model_requests": num_requests,
        "input_tokens": input_uncached + input_cached,  # TOTAL — do not bill
        "input_uncached_tokens": input_uncached,
        "input_cached_tokens": input_cached,
        "output_tokens": output,
        "input_text_tokens": input_uncached,
        "output_text_tokens": output,
        "input_cached_text_tokens": input_cached,
        "input_audio_tokens": 0,
        "input_cached_audio_tokens": 0,
        "output_audio_tokens": 0,
        "input_image_tokens": 0,
        "input_cached_image_tokens": 0,
        "output_image_tokens": 0,
    }


def _seed_openai_usage(
    tenant_id: str, today, days: int, email_map: dict[str, str], rng
) -> int:
    """Per-(day, user) OpenAI usage records over the trailing ``days``.

    Mirrors ``pull_openai_usage._normalize_result``: one record per
    grouped (model, user, key, project) row, ``record_type='usage'``,
    ``source_api='openai_admin_usage'``, the recon-pinned external_id
    ``openai:openai_admin_usage:{start_epoch}:{end_epoch}:{model}:{project}:{key}:{user}``,
    ``content_hash`` = SHA-256 of the canonical sub_meta, and the per-row
    wrapper metadata (window + modality + result + top-level
    subject_user_id / model / project_id / api_key_id / estimated_cost_usd
    / user_email). user_email is resolved from ``email_map`` (the seeded
    openai_users) so attribution stitches; the unmapped user yields no
    user_email and lands unmapped.

    Token counts get a small deterministic per-(user, day) jitter so the
    Users per-user spend isn't uniform. Dedup is by external_id (the epoch
    window is stable per UTC day), so same-day re-runs skip and later runs
    add the new day."""
    from datetime import datetime, timedelta, timezone

    from vargate_telemetry.pricing import openai_rates

    added = 0
    # Each roster user is assigned a stable key+project so their usage
    # attributes to a consistent project (the "workspace" dimension).
    for u_idx, (name, handle) in enumerate(_VOL_ROSTER):
        user_id = _openai_user_id(handle)
        api_key_id, project_id, _kname = _OPENAI_API_KEYS[
            u_idx % len(_OPENAI_API_KEYS)
        ]
        email = email_map.get(user_id)
        for d in range(days):
            # ~55% of days active per user (deterministic via seeded rng).
            if rng.random() > 0.55:
                continue
            day = today - timedelta(days=d)
            start = datetime(
                day.year, day.month, day.day, tzinfo=timezone.utc
            )
            end = start + timedelta(days=1)
            start_epoch = int(start.timestamp())
            end_epoch = int(end.timestamp())
            window = {
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "modality": "completions",
            }
            for model, base_unc, base_cache, base_out, base_req in (
                _OPENAI_MODELS
            ):
                # Deterministic jitter (0.6x–1.4x) so per-user totals vary.
                jitter = 0.6 + rng.random() * 0.8
                input_uncached = int(base_unc * jitter)
                input_cached = int(base_cache * jitter)
                output = int(base_out * jitter)
                num_requests = max(1, int(base_req * jitter))

                result_dict = _openai_usage_result_dict(
                    model=model,
                    user_id=user_id,
                    api_key_id=api_key_id,
                    project_id=project_id,
                    input_uncached=input_uncached,
                    input_cached=input_cached,
                    output=output,
                    num_requests=num_requests,
                )

                # §2.1 double-count-safe estimate — uncached as input,
                # cached as cache_read, no cache-creation charge.
                est = openai_rates.compute_cost_usd(
                    model,
                    input_tokens=input_uncached,
                    output_tokens=output,
                    cache_read_tokens=input_cached,
                    cache_creation_tokens=0,
                    occurred_at=start,
                )

                sub_meta: dict[str, Any] = {
                    **window,
                    "result": result_dict,
                    "subject_user_id": user_id,
                    "model": model,
                    "project_id": project_id,
                    "api_key_id": api_key_id,
                    "estimated_cost_usd": (
                        str(est) if est is not None else None
                    ),
                }
                if email is not None:
                    sub_meta["user_email"] = email

                ext = (
                    f"demo:openai:{SOURCE_API_OPENAI_USAGE}:"
                    f"{start_epoch}:{end_epoch}:"
                    f"{model}:{project_id}:{api_key_id}:{user_id}"
                )
                canonical = json.dumps(
                    sub_meta, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                try:
                    append_telemetry_record(
                        tenant_id,
                        record_type="usage",
                        source_api=SOURCE_API_OPENAI_USAGE,
                        external_id=ext,
                        occurred_at=start,
                        content_hash=hashlib.sha256(canonical).digest(),
                        record_metadata=sub_meta,
                        subject_user_id=user_id,
                    )
                    added += 1
                except IntegrityError:
                    pass

    # A slice of usage for the UNMAPPED service-account identity (no
    # email) on a few recent days so the Unmapped panel has real volume.
    for d in range(min(days, 5)):
        day = today - timedelta(days=d)
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        start_epoch = int(start.timestamp())
        end_epoch = int(end.timestamp())
        model = "gpt-4o-mini-2024-07-18"
        api_key_id, project_id, _ = _OPENAI_API_KEYS[1]  # nightly-batch key
        result_dict = _openai_usage_result_dict(
            model=model,
            user_id=_OPENAI_UNMAPPED_USER_ID,
            api_key_id=api_key_id,
            project_id=project_id,
            input_uncached=52000,
            input_cached=18000,
            output=14000,
            num_requests=80,
        )
        est = openai_rates.compute_cost_usd(
            model,
            input_tokens=52000,
            output_tokens=14000,
            cache_read_tokens=18000,
            cache_creation_tokens=0,
            occurred_at=start,
        )
        sub_meta = {
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "modality": "completions",
            "result": result_dict,
            "subject_user_id": _OPENAI_UNMAPPED_USER_ID,
            "model": model,
            "project_id": project_id,
            "api_key_id": api_key_id,
            "estimated_cost_usd": str(est) if est is not None else None,
        }
        ext = (
            f"demo:openai:{SOURCE_API_OPENAI_USAGE}:"
            f"{start_epoch}:{end_epoch}:"
            f"{model}:{project_id}:{api_key_id}:{_OPENAI_UNMAPPED_USER_ID}"
        )
        canonical = json.dumps(
            sub_meta, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        try:
            append_telemetry_record(
                tenant_id,
                record_type="usage",
                source_api=SOURCE_API_OPENAI_USAGE,
                external_id=ext,
                occurred_at=start,
                content_hash=hashlib.sha256(canonical).digest(),
                record_metadata=sub_meta,
                subject_user_id=_OPENAI_UNMAPPED_USER_ID,
            )
            added += 1
        except IntegrityError:
            pass

    return added


def _seed_openai_costs(tenant_id: str, today, days: int) -> int:
    """Authoritative per-project daily OpenAI spend over ``days``.

    Mirrors ``pull_openai_costs._normalize_cost_result``: one record per
    (line_item, project) row, ``record_type='cost'``,
    ``source_api='openai_admin_costs'``, external_id
    ``openai:openai_admin_costs:{start}:{end}:{line_item}:{project}``,
    top-level ``amount_value`` (Decimal string — the field spend_data /
    cost_forecasting sum on), ``line_item`` / ``project_id`` /
    ``project_name`` / ``currency``, and the full ``result`` dict
    (CostResult.model_dump shape). gpt-4o + gpt-4o-mini input/output line
    items per project per day."""
    from datetime import datetime, timedelta, timezone

    added = 0
    # Per-project, per-model daily billed amount (USD). gpt-4o costs more;
    # split into input/output line items the way /costs reports them.
    # (project_id, project_name, [(model, input_usd, output_usd)])
    cost_plan = [
        (
            "proj_demo_platform",
            "Platform",
            [
                ("gpt-4o-2024-08-06", "0.62", "0.48"),
                ("gpt-4o-mini-2024-07-18", "0.18", "0.16"),
            ],
        ),
        (
            "proj_demo_apps",
            "Applications",
            [
                ("gpt-4o-2024-08-06", "0.34", "0.27"),
                ("gpt-4o-mini-2024-07-18", "0.11", "0.09"),
            ],
        ),
    ]
    for d in range(days):
        day = today - timedelta(days=d)
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        start_epoch = int(start.timestamp())
        end_epoch = int(end.timestamp())
        window = {
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        }
        for project_id, project_name, line_items in cost_plan:
            for model, in_usd, out_usd in line_items:
                for kind, amount in (("input", in_usd), ("output", out_usd)):
                    line_item = f"{model}, {kind}"
                    result_dict = {
                        "object": "organization.costs.result",
                        "amount": {"value": float(amount), "currency": "usd"},
                        "line_item": line_item,
                        "quantity": None,
                        "project_id": project_id,
                        "project_name": project_name,
                        "organization_id": None,
                        "organization_name": None,
                    }
                    sub_meta = {
                        **window,
                        "result": result_dict,
                        "line_item": line_item,
                        "project_id": project_id,
                        "project_name": project_name,
                        "amount_value": amount,
                        "currency": "usd",
                    }
                    ext = (
                        f"demo:openai:{SOURCE_API_OPENAI_COSTS}:"
                        f"{start_epoch}:{end_epoch}:{line_item}:{project_id}"
                    )
                    canonical = json.dumps(
                        sub_meta, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                    try:
                        append_telemetry_record(
                            tenant_id,
                            record_type="cost",
                            source_api=SOURCE_API_OPENAI_COSTS,
                            external_id=ext,
                            occurred_at=start,
                            content_hash=hashlib.sha256(canonical).digest(),
                            record_metadata=sub_meta,
                        )
                        added += 1
                    except IntegrityError:
                        pass
    return added


def _seed_openai_audit(tenant_id: str, today) -> int:
    """A handful of OpenAI audit-log records.

    Mirrors ``pull_openai_audit._normalize_audit``: ``record_type=
    'audit_log'``, ``source_api='openai_audit_logs'``, external_id
    ``openai:openai_audit_logs:{event_id}`` (the stable event id is the
    dedup key — no window), the full entry under ``metadata['entry']``
    (AuditLogEntry.model_dump shape) + top-level event_type /
    subject_user_id (+ user_email when the actor has one). Events are
    relative-dated to the last few days."""
    from datetime import datetime, timedelta, timezone

    # (event_id_suffix, event_type, days_ago, actor_handle_or_None)
    events = [
        ("apikeycreated", "api_key.created", 1, "carol"),
        ("loginsucceeded", "login.succeeded", 1, "dave"),
        ("projectupdated", "project.updated", 2, "erin"),
        ("invitesent", "invite.sent", 2, "frank"),
        ("logoutsucceeded", "logout.succeeded", 3, "grace"),
    ]
    added = 0
    for suffix, event_type, days_ago, handle in events:
        day = today - timedelta(days=days_ago)
        occurred = datetime(
            day.year, day.month, day.day, 12, tzinfo=timezone.utc
        )
        event_id = f"audit_log-demo-{suffix}"
        user_id = _openai_user_id(handle) if handle else None
        email = f"{handle}@{_VOL_DOMAIN}" if handle else None
        entry_dict: dict[str, Any] = {
            "object": "organization.audit_log",
            "id": event_id,
            "type": event_type,
            "effective_at": int(occurred.timestamp()),
            "actor": {
                "type": "session",
                "session": (
                    {"user": {"id": user_id, "email": email}}
                    if user_id
                    else None
                ),
                "api_key": None,
            },
            "project": {
                "id": "proj_demo_platform",
                "name": "Platform",
            },
        }
        sub_meta: dict[str, Any] = {
            "entry": entry_dict,
            "event_type": event_type,
            "subject_user_id": user_id,
        }
        if email is not None:
            sub_meta["user_email"] = email
        ext = f"demo:openai:{SOURCE_API_OPENAI_AUDIT}:{event_id}"
        canonical = json.dumps(
            sub_meta, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        try:
            append_telemetry_record(
                tenant_id,
                record_type="audit_log",
                source_api=SOURCE_API_OPENAI_AUDIT,
                external_id=ext,
                occurred_at=occurred,
                content_hash=hashlib.sha256(canonical).digest(),
                record_metadata=sub_meta,
                subject_user_id=user_id,
            )
            added += 1
        except IntegrityError:
            pass
    return added


def seed_openai_volume(tenant_id: str, *, days: int = 30) -> dict[str, int]:
    """Seed OpenAI cross-vendor demo activity onto the roster.

    Layers the OpenAI side tables + per-(day, user) usage + per-project
    costs + a few audit logs onto the SAME ``_VOL_ROSTER`` users
    ``seed_volume`` creates, so the cross-vendor dashboard renders OpenAI
    next to Anthropic. Idempotent (``demo:`` external_ids), relative-dated
    to today (nightly cron keeps it inside the 7d/30d windows).

    Calls ``ensure_tenant`` + creates the roster users itself, so it is
    independently runnable (``scripts/seed_demo_openai.py``) without first
    running the Anthropic ``seed_volume``. When invoked FROM
    ``seed_volume`` the roster users already exist (``_ensure_user`` is
    idempotent), so no duplication.

    Returns per-stream added-row counts."""
    import random
    from datetime import date

    if not tenant_id:
        raise ValueError("tenant_id required")
    ensure_tenant(tenant_id)

    # Ensure the roster users exist (idempotent) so OpenAI attribution has
    # users.email rows to stitch into even when run standalone.
    for name, handle in _VOL_ROSTER:
        _ensure_user(tenant_id, f"{handle}@{_VOL_DOMAIN}", name, handle)

    side = _seed_openai_side_tables(tenant_id)
    # Resolve the seeded openai_users → email map (what the pull task's
    # _load_email_map builds) so usage records carry metadata.user_email.
    email_map = {
        _openai_user_id(handle): f"{handle}@{_VOL_DOMAIN}"
        for _name, handle in _VOL_ROSTER
    }

    rng = random.Random(8675309)  # distinct stream from seed_volume's rng
    today = date.today()

    usage_added = _seed_openai_usage(tenant_id, today, days, email_map, rng)
    costs_added = _seed_openai_costs(tenant_id, today, days)
    audit_added = _seed_openai_audit(tenant_id, today)

    return {
        "projects_added": side["projects_added"],
        "api_keys_added": side["api_keys_added"],
        "users_added": side["users_added"],
        "usage_added": usage_added,
        "costs_added": costs_added,
        "audit_added": audit_added,
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
