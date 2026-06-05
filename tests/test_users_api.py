# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the cross-surface users API (TM3 Phase C2).

Covers:
  - GET /api/users — roster with surfaces + 7d events + spend +
    unmapped panel; lazy reconcile stitches on read; RLS isolation.
  - GET /api/users/{id} — detail with heatmap cells + spend trend +
    recent records; 404 on unknown user.
  - POST /api/users/{id}/aliases — manual map sets auto_matched=false;
    404 on unknown user.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

os.environ.setdefault(
    "JWT_SIGNING_KEY",
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b",
)


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


@pytest.fixture
def clean_state() -> Iterator[None]:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE user_aliases RESTART IDENTITY CASCADE")
        )
        conn.execute(
            sql_text("TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE")
        )
    yield
    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE user_aliases RESTART IDENTITY CASCADE")
        )
        conn.execute(
            sql_text("TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE")
        )


def _provision_tenant(tenant_id: str) -> None:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO tenants (tenant_id, region, active, billing_status)
                VALUES (:t, 'us', TRUE, 'trial')
                ON CONFLICT (tenant_id) DO NOTHING
                """
            ),
            {"t": tenant_id},
        )


def _provision_user(
    tenant_id: str, email: str, role: str = "member"
) -> str:
    uid = str(uuid.uuid4())
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO users
                    (id, email, sso_provider, sso_subject_id, tenant_id, role)
                VALUES (:id, :email, 'google', :sub, :t, :role)
                """
            ),
            {
                "id": uid,
                "email": email,
                "sub": f"sub-{uid}",
                "t": tenant_id,
                "role": role,
            },
        )
    return uid


def _bearer(tenant_id: str | None, user_id: str | None = None) -> dict:
    from vargate_telemetry.auth.jwt import issue_session_jwt

    # TM4: alias mapping is admin-gated and require_admin looks the
    # caller's role up in the DB. When a test doesn't pin an identity,
    # auto-provision a real admin caller so the default authenticated
    # client can perform admin actions (mirrors test_budgets_api). Tests
    # that need a member caller pass an explicit member user_id.
    if user_id is None and tenant_id is not None:
        user_id = _provision_user(
            tenant_id,
            f"caller-{uuid.uuid4().hex[:8]}@example.com",
            role="admin",
        )

    token = issue_session_jwt(
        user_id=user_id or str(uuid.uuid4()),
        email="probe@example.com",
        sso_provider="google",
        tenant_id=tenant_id,
    )
    return {"Authorization": f"Bearer {token}"}


_SONNET = "claude-sonnet-4-5-20250929"


def _seed_code_analytics(
    tenant_id: str, *, email: str, occurred_at: datetime | None = None
) -> None:
    md = {"actor": {"type": "user_actor", "email_address": email}}
    _insert(tenant_id, "code_analytics", md, occurred_at)


def _seed_mcp(
    tenant_id: str,
    *,
    email: str,
    user_id: str,
    occurred_at: datetime | None = None,
    input_tokens: int = 1_000_000,
    output_tokens: int = 200_000,
    model: str = _SONNET,
    kind: str = "chat",
    surface: str | None = None,
) -> None:
    md = {
        "kind": kind,
        "summary": "Discussed the quarterly roadmap.",
        "model": model,
        "input_tokens_estimate": input_tokens,
        "output_tokens_estimate": output_tokens,
        "user_email": email,
        "subject_user_id": user_id,
    }
    if surface is not None:
        md["surface"] = surface
    _insert(tenant_id, "mcp", md, occurred_at)


# TM8 Phase E — OpenAI per-user usage record. Mirrors what
# pull_openai_usage writes: a per-row wrapper with the grouped `result`
# (the priceable token split) and the cross-vendor attribution keys
# (`user_email` when the tier resolves an email, else just
# `subject_user_id`). The ACTOR_KEY_SQL COALESCE matches on `user_email`
# first, falling back to `subject_user_id` for an unmapped identity.
_GPT4O = "gpt-4o-2024-08-06"


def _seed_openai_usage(
    tenant_id: str,
    *,
    subject_user_id: str,
    email: str | None = None,
    occurred_at: datetime | None = None,
    input_uncached_tokens: int = 1_000_000,
    input_cached_tokens: int = 0,
    output_tokens: int = 1_000_000,
    model: str = _GPT4O,
) -> None:
    # `input_tokens` is the TOTAL (uncached + cached) — present so the
    # test exercises the double-count trap: cost must derive from the
    # split, never this raw total.
    result = {
        "object": "organization.usage.completions.result",
        "model": model,
        "user_id": subject_user_id,
        "input_tokens": input_uncached_tokens + input_cached_tokens,
        "input_uncached_tokens": input_uncached_tokens,
        "input_cached_tokens": input_cached_tokens,
        "output_tokens": output_tokens,
    }
    md: dict = {
        "start_time": "2026-06-01T00:00:00+00:00",
        "end_time": "2026-06-02T00:00:00+00:00",
        "modality": "completions",
        "result": result,
        "subject_user_id": subject_user_id,
        "model": model,
    }
    if email is not None:
        md["user_email"] = email
    _insert(tenant_id, "openai_admin_usage", md, occurred_at)


def _insert(
    tenant_id: str, source_api: str, md: dict, occurred_at: datetime | None
) -> None:
    from vargate_telemetry.db import engine

    occurred_at = occurred_at or datetime.now(tz=timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, :source_api, :source_api, :eid,
                    :occurred_at, decode(:zero32, 'hex'),
                    :metadata,
                    (SELECT COALESCE(MAX(chain_seq), 0) + 1
                       FROM telemetry_records WHERE tenant_id = :t_lookup),
                    decode(:zero32, 'hex'), decode(:one32, 'hex')
                )
                """
            ),
            {
                "t": tenant_id,
                "t_lookup": tenant_id,
                "source_api": source_api,
                "eid": f"{source_api}:{uuid.uuid4()}",
                "occurred_at": occurred_at,
                "metadata": json.dumps(md),
                "zero32": "00" * 32,
                "one32": "11" * 32,
            },
        )


# ───────────────────────────────────────────────────────────────────────────
# GET /api/users
# ───────────────────────────────────────────────────────────────────────────


def test_list_users_stitches_across_surfaces(
    clean_state: None, client: TestClient
) -> None:
    """A user active on both Code Analytics + MCP shows ONE row with
    both surfaces — the core cross-surface analytic."""
    tenant = "tnt_us_users_stitch"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "rick@vargate.ai")
    _seed_code_analytics(tenant, email="rick@vargate.ai")
    _seed_mcp(tenant, email="rick@vargate.ai", user_id=uid)

    r = client.get("/users", headers=_bearer(tenant))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["users"]) == 1
    row = body["users"][0]
    assert row["email"] == "rick@vargate.ai"
    # Both surfaces present (lazy reconcile auto-matched on read).
    assert set(row["surfaces"]) == {"code_analytics", "mcp"}
    assert row["events_7d"] == 2


def test_list_users_computes_mcp_spend(
    clean_state: None, client: TestClient
) -> None:
    tenant = "tnt_us_users_spend"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "spend@example.com")
    # 1M in + 200k out at Sonnet = $6.00.
    _seed_mcp(tenant, email="spend@example.com", user_id=uid)

    r = client.get("/users", headers=_bearer(tenant))
    row = r.json()["users"][0]
    assert Decimal(row["spend_7d_usd"]) == Decimal("6.00")


def test_list_users_spend_none_when_no_priceable_activity(
    clean_state: None, client: TestClient
) -> None:
    """A user with only Code Analytics activity (no priceable MCP
    tokens) gets spend=None — rendered as '—', never faked $0."""
    tenant = "tnt_us_users_nospend"
    _provision_tenant(tenant)
    _provision_user(tenant, "ca@example.com")
    _seed_code_analytics(tenant, email="ca@example.com")

    r = client.get("/users", headers=_bearer(tenant))
    row = r.json()["users"][0]
    assert row["spend_7d_usd"] is None


def test_list_users_surfaces_unmapped_activity(
    clean_state: None, client: TestClient
) -> None:
    """An api_key_name actor (no matching user) shows in the unmapped
    panel, not the user roster."""
    tenant = "tnt_us_users_unmapped"
    _provision_tenant(tenant)
    # No user. A Code Analytics api_actor.
    md = {"actor": {"type": "api_actor", "api_key_name": "sera-production"}}
    _insert(tenant, "code_analytics", md, None)

    r = client.get("/users", headers=_bearer(tenant))
    body = r.json()
    assert body["users"] == []
    assert len(body["unmapped"]) == 1
    assert body["unmapped"][0]["source_identifier"] == "sera-production"
    assert body["unmapped"][0]["event_count"] == 1


# ───────────────────────────────────────────────────────────────────────────
# TM8 Phase E — cross-vendor user view (one person, both vendors)
# ───────────────────────────────────────────────────────────────────────────


def test_list_users_stitches_anthropic_and_openai(
    clean_state: None, client: TestClient
) -> None:
    """A user active on BOTH Claude (MCP) and OpenAI usage shows ONE row
    carrying both surfaces — the headline cross-vendor analytic. The
    OpenAI activity stitches into the same user via email auto-match."""
    tenant = "tnt_us_users_xvendor_stitch"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "both@example.com")
    _seed_mcp(tenant, email="both@example.com", user_id=uid)
    _seed_openai_usage(
        tenant, subject_user_id="user-openai-1", email="both@example.com"
    )

    body = client.get("/users", headers=_bearer(tenant)).json()
    assert len(body["users"]) == 1, body["users"]
    row = body["users"][0]
    assert row["email"] == "both@example.com"
    # Both vendor surfaces present: 'mcp' (Anthropic) +
    # 'openai_admin_usage' (OpenAI). The frontend maps each token →
    # vendor via sourceVendor().
    assert set(row["surfaces"]) == {"mcp", "openai_admin_usage"}
    assert row["events_7d"] == 2


def test_list_users_spend_sums_both_vendors(
    clean_state: None, client: TestClient
) -> None:
    """Per-user spend_7d_usd is the CROSS-VENDOR total (Anthropic MCP +
    OpenAI usage), and spend_7d_by_vendor carries the per-vendor split.
    MCP: 1M in + 200k out @ Sonnet = $6.00. OpenAI gpt-4o: 1M uncached
    in + 1M out = $12.50. Total = $18.50."""
    tenant = "tnt_us_users_xvendor_spend"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "spend@example.com")
    _seed_mcp(tenant, email="spend@example.com", user_id=uid)
    _seed_openai_usage(
        tenant, subject_user_id="user-openai-2", email="spend@example.com"
    )

    row = client.get("/users", headers=_bearer(tenant)).json()["users"][0]
    assert Decimal(row["spend_7d_usd"]) == Decimal("18.50")
    by_vendor = row["spend_7d_by_vendor"]
    assert Decimal(by_vendor["Anthropic"]) == Decimal("6.00")
    assert Decimal(by_vendor["OpenAI"]) == Decimal("12.50")
    # The per-vendor split sums to the headline total.
    assert sum(Decimal(v) for v in by_vendor.values()) == Decimal(
        row["spend_7d_usd"]
    )


def test_list_users_openai_spend_is_double_count_safe(
    clean_state: None, client: TestClient
) -> None:
    """OpenAI per-user spend derives from the uncached/cached split, NOT
    the raw input_tokens total. gpt-4o with 1M uncached + 1M cached + 0
    out = $2.50 + $1.25 = $3.75 (NOT 2M × $2.50 = $5.00 if it billed the
    raw total)."""
    tenant = "tnt_us_users_openai_doublecount"
    _provision_tenant(tenant)
    _provision_user(tenant, "cached@example.com")
    _seed_openai_usage(
        tenant,
        subject_user_id="user-openai-3",
        email="cached@example.com",
        input_uncached_tokens=1_000_000,
        input_cached_tokens=1_000_000,
        output_tokens=0,
    )

    row = client.get("/users", headers=_bearer(tenant)).json()["users"][0]
    assert Decimal(row["spend_7d_usd"]) == Decimal("3.75")
    assert Decimal(row["spend_7d_by_vendor"]["OpenAI"]) == Decimal("3.75")
    assert "Anthropic" not in row["spend_7d_by_vendor"]


def test_list_users_anthropic_only_unchanged(
    clean_state: None, client: TestClient
) -> None:
    """Regression: an Anthropic-only user (MCP, no OpenAI) is byte-for-
    byte what TM3 produced — spend_7d_usd is the MCP total and
    spend_7d_by_vendor has only the Anthropic key."""
    tenant = "tnt_us_users_anthropic_only"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "anthropic@example.com")
    _seed_mcp(tenant, email="anthropic@example.com", user_id=uid)

    row = client.get("/users", headers=_bearer(tenant)).json()["users"][0]
    assert set(row["surfaces"]) == {"mcp"}
    assert Decimal(row["spend_7d_usd"]) == Decimal("6.00")
    assert row["spend_7d_by_vendor"] == {"Anthropic": "6.00"}


def test_list_users_openai_attributes_to_email_matched_user(
    clean_state: None, client: TestClient
) -> None:
    """OpenAI usage whose resolved user_email matches a users.email row
    attributes to THAT user (email auto-match), not the unmapped panel."""
    tenant = "tnt_us_users_openai_match"
    _provision_tenant(tenant)
    _provision_user(tenant, "matched@example.com")
    _seed_openai_usage(
        tenant, subject_user_id="user-openai-4", email="matched@example.com"
    )

    body = client.get("/users", headers=_bearer(tenant)).json()
    assert len(body["users"]) == 1
    assert body["users"][0]["email"] == "matched@example.com"
    assert body["users"][0]["surfaces"] == ["openai_admin_usage"]
    # Nothing left unmapped — the email resolved.
    assert body["unmapped"] == []


def test_list_users_surfaces_unmapped_openai_identity(
    clean_state: None, client: TestClient
) -> None:
    """An OpenAI usage row with NO resolvable email (only a raw
    subject_user_id — the no-user-level-email tier) lands in the unmapped
    panel keyed on that id, vendor-labelable via its source_api."""
    tenant = "tnt_us_users_openai_unmapped"
    _provision_tenant(tenant)
    # No matching user; no user_email on the record.
    _seed_openai_usage(tenant, subject_user_id="user-openai-orphan")

    body = client.get("/users", headers=_bearer(tenant)).json()
    assert body["users"] == []
    assert len(body["unmapped"]) == 1
    um = body["unmapped"][0]
    assert um["source_api"] == "openai_admin_usage"
    assert um["source_identifier"] == "user-openai-orphan"
    assert um["event_count"] == 1


def test_user_detail_cross_vendor_spend_trend(
    clean_state: None, client: TestClient
) -> None:
    """Detail spend_trend is vendor-stacked: a user with MCP activity on
    one day and OpenAI activity on another yields per-(day, source) points,
    each carrying its display vendor. The heatmap + recent timeline
    include the OpenAI source rows too."""
    tenant = "tnt_us_users_xvendor_detail"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "detail@example.com")
    now = datetime.now(tz=timezone.utc)
    _seed_mcp(
        tenant,
        email="detail@example.com",
        user_id=uid,
        occurred_at=now - timedelta(days=1),
    )
    _seed_openai_usage(
        tenant,
        subject_user_id="user-openai-5",
        email="detail@example.com",
        occurred_at=now - timedelta(days=2),
    )
    client.get("/users", headers=_bearer(tenant))  # lazy reconcile

    body = client.get(f"/users/{uid}", headers=_bearer(tenant)).json()
    # Both vendor surfaces in the header.
    assert set(body["surfaces"]) == {"mcp", "openai_admin_usage"}
    # Heatmap carries both source rows.
    heat_sources = {c["source_api"] for c in body["heatmap"]}
    assert heat_sources == {"mcp", "openai_admin_usage"}
    # Spend trend: one point per vendor, each tagged with its vendor +
    # source_api. MCP = $6.00 (Anthropic); OpenAI gpt-4o = $12.50.
    by_vendor = {p["vendor"]: p for p in body["spend_trend"]}
    assert set(by_vendor) == {"Anthropic", "OpenAI"}
    assert by_vendor["Anthropic"]["source_api"] == "mcp"
    assert Decimal(by_vendor["Anthropic"]["spend_usd"]) == Decimal("6.00")
    assert by_vendor["OpenAI"]["source_api"] == "openai_admin_usage"
    assert Decimal(by_vendor["OpenAI"]["spend_usd"]) == Decimal("12.50")
    # Recent timeline mixes both sources.
    recent_sources = {r["source_api"] for r in body["recent"]}
    assert recent_sources == {"mcp", "openai_admin_usage"}


def test_user_detail_anthropic_only_spend_trend_unchanged(
    clean_state: None, client: TestClient
) -> None:
    """Regression: an Anthropic-only user's detail spend_trend is the TM3
    shape — one point per day, source_api='mcp' — with the additive
    'vendor'='Anthropic' field. Single MCP day = one point at $6.00."""
    tenant = "tnt_us_users_detail_anthropic_only"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "solo@example.com")
    _seed_mcp(
        tenant,
        email="solo@example.com",
        user_id=uid,
        occurred_at=datetime.now(tz=timezone.utc) - timedelta(days=1),
    )
    client.get("/users", headers=_bearer(tenant))

    body = client.get(f"/users/{uid}", headers=_bearer(tenant)).json()
    assert len(body["spend_trend"]) == 1
    pt = body["spend_trend"][0]
    assert pt["source_api"] == "mcp"
    assert pt["vendor"] == "Anthropic"
    assert Decimal(pt["spend_usd"]) == Decimal("6.00")


# ───────────────────────────────────────────────────────────────────────────
# TM4 #3 — Claude Code vs Claude (chat) surface delineation
# ───────────────────────────────────────────────────────────────────────────


def test_list_users_surface_self_reported_claude_code(
    clean_state: None, client: TestClient
) -> None:
    """An MCP record self-reporting surface=claude_code surfaces as
    'claude_code' (rendered 'Claude Code'), not the bare 'mcp'."""
    tenant = "tnt_us_users_surface_code"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "coder@example.com")
    _seed_mcp(
        tenant, email="coder@example.com", user_id=uid, surface="claude_code"
    )

    r = client.get("/users", headers=_bearer(tenant))
    row = r.json()["users"][0]
    assert row["surfaces"] == ["claude_code"]


def test_list_users_surface_kind_tool_use_fallback(
    clean_state: None, client: TestClient
) -> None:
    """A pre-surface MCP record (no `surface` field) with kind=tool_use
    is retro-labeled claude_code via the read-path heuristic — the
    immediate win for data captured before the field shipped."""
    tenant = "tnt_us_users_surface_kindfallback"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "legacy@example.com")
    _seed_mcp(
        tenant, email="legacy@example.com", user_id=uid, kind="tool_use"
    )  # no surface

    r = client.get("/users", headers=_bearer(tenant))
    row = r.json()["users"][0]
    assert row["surfaces"] == ["claude_code"]


def test_list_users_surface_plain_chat_stays_mcp(
    clean_state: None, client: TestClient
) -> None:
    """An MCP record with no surface and kind=chat stays 'mcp' (rendered
    'Claude (chat)') — the heuristic must not over-claim Claude Code."""
    tenant = "tnt_us_users_surface_chat"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "chatter@example.com")
    _seed_mcp(tenant, email="chatter@example.com", user_id=uid)  # kind=chat

    r = client.get("/users", headers=_bearer(tenant))
    row = r.json()["users"][0]
    assert row["surfaces"] == ["mcp"]


def test_user_detail_surfaces_delineate_claude_code(
    clean_state: None, client: TestClient
) -> None:
    """Detail header surfaces reflect the effective surface: a user with
    one claude_code MCP turn + one chat MCP turn shows both badges."""
    tenant = "tnt_us_users_surface_detail"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "mixed@example.com")
    now = datetime.now(tz=timezone.utc)
    _seed_mcp(
        tenant,
        email="mixed@example.com",
        user_id=uid,
        surface="claude_code",
        occurred_at=now - timedelta(days=1),
    )
    _seed_mcp(
        tenant,
        email="mixed@example.com",
        user_id=uid,
        occurred_at=now - timedelta(days=2),
    )  # plain chat
    client.get("/users", headers=_bearer(tenant))  # lazy reconcile

    body = client.get(f"/users/{uid}", headers=_bearer(tenant)).json()
    assert set(body["surfaces"]) == {"claude_code", "mcp"}
    # The newest record (claude_code) leads the recent list.
    assert body["recent"][0]["source_api"] == "claude_code"


# ───────────────────────────────────────────────────────────────────────────
# TM4 Track D — Top topics on the user-detail view
# ───────────────────────────────────────────────────────────────────────────


def _seed_topics(tenant_id: str, assignments: list[tuple[str, int]]) -> None:
    """Stand in for the async classifier: assign topics to this tenant's
    MCP records (round-robin over their ids). ``assignments`` is a list
    of ``(topic, count)``."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        ids = [
            r.id
            for r in conn.execute(
                sql_text(
                    "SELECT id::text AS id FROM telemetry_records "
                    "WHERE tenant_id = :t AND source_api = 'mcp' "
                    "ORDER BY id"
                ),
                {"t": tenant_id},
            ).all()
        ]
        i = 0
        for topic, count in assignments:
            for _ in range(count):
                conn.execute(
                    sql_text(
                        "INSERT INTO interaction_topics (tenant_id, "
                        "record_id, topic, taxonomy_version, model) "
                        "VALUES (:t, :rid, :topic, 'v1', 'test')"
                    ),
                    {"t": tenant_id, "rid": ids[i], "topic": topic},
                )
                i += 1


def test_user_detail_top_topics(
    clean_state: None, client: TestClient
) -> None:
    """/users/{id} returns ranked Top topics + classified/total counts."""
    tenant = "tnt_us_users_topics"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "topics@example.com")
    for _ in range(3):
        _seed_mcp(tenant, email="topics@example.com", user_id=uid)
    client.get("/users", headers=_bearer(tenant))  # reconcile the alias
    _seed_topics(tenant, [("Coding", 2), ("Research", 1)])

    body = client.get(f"/users/{uid}", headers=_bearer(tenant)).json()
    # Ranked by count desc, ties by topic name.
    assert body["top_topics"] == [
        {"topic": "Coding", "count": 2},
        {"topic": "Research", "count": 1},
    ]
    assert body["topics_classified"] == 3
    assert body["topics_total"] == 3  # 3 MCP records, all with summaries


def test_user_detail_top_topics_empty_when_unclassified(
    clean_state: None, client: TestClient
) -> None:
    """No classifications yet → empty top_topics + classified 0, but
    topics_total still reflects the classifiable (summarized) MCP
    records, so the UI can show 'N of M classified'."""
    tenant = "tnt_us_users_topics_empty"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "pending@example.com")
    _seed_mcp(tenant, email="pending@example.com", user_id=uid)
    client.get("/users", headers=_bearer(tenant))

    body = client.get(f"/users/{uid}", headers=_bearer(tenant)).json()
    assert body["top_topics"] == []
    assert body["topics_classified"] == 0
    assert body["topics_total"] == 1


def test_list_users_rls_isolated(
    clean_state: None, client: TestClient
) -> None:
    tenant_a = "tnt_us_users_rls_a"
    tenant_b = "tnt_us_users_rls_b"
    _provision_tenant(tenant_a)
    _provision_tenant(tenant_b)
    _provision_user(tenant_a, "a@example.com")
    _provision_user(tenant_b, "b@example.com")
    _seed_code_analytics(tenant_a, email="a@example.com")
    _seed_code_analytics(tenant_b, email="b@example.com")

    rows_a = client.get("/users", headers=_bearer(tenant_a)).json()["users"]
    assert len(rows_a) == 1
    assert rows_a[0]["email"] == "a@example.com"


def test_list_users_no_tenant_bound_400(
    clean_state: None, client: TestClient
) -> None:
    r = client.get("/users", headers=_bearer(None))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "no_tenant_bound"


# ───────────────────────────────────────────────────────────────────────────
# GET /api/users/{id}
# ───────────────────────────────────────────────────────────────────────────


def test_user_detail_returns_heatmap_spend_and_recent(
    clean_state: None, client: TestClient
) -> None:
    tenant = "tnt_us_users_detail"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "detail@example.com")
    now = datetime.now(tz=timezone.utc)
    _seed_mcp(
        tenant,
        email="detail@example.com",
        user_id=uid,
        occurred_at=now - timedelta(days=1),
    )
    _seed_code_analytics(
        tenant, email="detail@example.com", occurred_at=now - timedelta(days=2)
    )
    # Reconcile via the list endpoint first (lazy reconcile).
    client.get("/users", headers=_bearer(tenant))

    r = client.get(f"/users/{uid}", headers=_bearer(tenant))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "detail@example.com"
    assert set(body["surfaces"]) == {"code_analytics", "mcp"}
    # Two aliases (one per surface).
    assert len(body["aliases"]) == 2
    # Heatmap has cells for both days/sources.
    assert len(body["heatmap"]) == 2
    # Spend trend has the MCP day.
    assert len(body["spend_trend"]) == 1
    assert Decimal(body["spend_trend"][0]["spend_usd"]) == Decimal("6.00")
    # Recent activity, newest first.
    assert len(body["recent"]) == 2
    assert body["recent"][0]["source_api"] == "mcp"  # day-1 newest


def test_user_detail_404_on_unknown_user(
    clean_state: None, client: TestClient
) -> None:
    tenant = "tnt_us_users_detail_404"
    _provision_tenant(tenant)
    r = client.get(f"/users/{uuid.uuid4()}", headers=_bearer(tenant))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "user_not_found"


# ───────────────────────────────────────────────────────────────────────────
# POST /api/users/{id}/aliases
# ───────────────────────────────────────────────────────────────────────────


def test_manual_alias_map_sets_auto_matched_false(
    clean_state: None, client: TestClient
) -> None:
    tenant = "tnt_us_users_map"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "map@example.com")
    # An unmapped api_key actor that the admin wants to attribute to
    # this user.
    md = {"actor": {"type": "api_actor", "api_key_name": "rick-laptop"}}
    _insert(tenant, "code_analytics", md, None)
    client.get("/users", headers=_bearer(tenant))  # reconcile → unmapped

    r = client.post(
        f"/users/{uid}/aliases",
        json={
            "source_api": "code_analytics",
            "source_identifier": "rick-laptop",
        },
        headers=_bearer(tenant),
    )
    assert r.status_code == 201, r.text
    assert r.json()["auto_matched"] is False

    # Now the user roster shows the previously-unmapped activity, and
    # the unmapped panel is empty.
    body = client.get("/users", headers=_bearer(tenant)).json()
    assert len(body["users"]) == 1
    assert body["unmapped"] == []


def test_manual_alias_map_404_on_unknown_user(
    clean_state: None, client: TestClient
) -> None:
    tenant = "tnt_us_users_map_404"
    _provision_tenant(tenant)
    r = client.post(
        f"/users/{uuid.uuid4()}/aliases",
        json={"source_api": "mcp", "source_identifier": "x@example.com"},
        headers=_bearer(tenant),
    )
    assert r.status_code == 404


def test_manual_alias_map_survives_reconcile(
    clean_state: None, client: TestClient
) -> None:
    """After a manual map, a subsequent lazy reconcile (on the next
    /users GET) must NOT un-map or re-point it."""
    tenant = "tnt_us_users_map_persist"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "persist@example.com")
    # Telemetry actor email is different from the user's email, so
    # auto-match would NOT link it. Admin links manually.
    _seed_code_analytics(tenant, email="alias-only@example.com")
    client.get("/users", headers=_bearer(tenant))  # → unmapped

    client.post(
        f"/users/{uid}/aliases",
        json={
            "source_api": "code_analytics",
            "source_identifier": "alias-only@example.com",
        },
        headers=_bearer(tenant),
    )
    # Re-list (triggers reconcile). Manual link must hold.
    body = client.get("/users", headers=_bearer(tenant)).json()
    assert len(body["users"]) == 1
    assert body["users"][0]["email"] == "persist@example.com"
    assert body["unmapped"] == []


# ───────────────────────────────────────────────────────────────────────────
# TM4 — lightweight admin/member role gate + role management
#
# clean_state does NOT truncate users/tenants, so each test uses a
# globally-unique tenant_id; count_admins() then reflects only what the
# test provisions (the last-admin guard would be flaky otherwise).
# ───────────────────────────────────────────────────────────────────────────


def _uniq_tenant() -> str:
    return f"tnt_us_role_{uuid.uuid4().hex[:10]}"


def _role_in_db(tenant_id: str, user_id: str) -> str | None:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        return conn.execute(
            sql_text(
                "SELECT role FROM users WHERE id = :id AND tenant_id = :t"
            ),
            {"id": user_id, "t": tenant_id},
        ).scalar()


def test_me_reports_role(clean_state: None, client: TestClient) -> None:
    tenant = _uniq_tenant()
    _provision_tenant(tenant)
    admin = _provision_user(tenant, "admin@example.com", role="admin")
    member = _provision_user(tenant, "member@example.com", role="member")
    assert client.get("/me", headers=_bearer(tenant, admin)).json()["role"] == "admin"
    assert (
        client.get("/me", headers=_bearer(tenant, member)).json()["role"]
        == "member"
    )


def test_member_cannot_map_alias(clean_state: None, client: TestClient) -> None:
    tenant = _uniq_tenant()
    _provision_tenant(tenant)
    member = _provision_user(tenant, "member@example.com", role="member")
    target = _provision_user(tenant, "target@example.com", role="member")
    r = client.post(
        f"/users/{target}/aliases",
        json={"source_api": "mcp", "source_identifier": "x@example.com"},
        headers=_bearer(tenant, member),
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "admin_required"


def test_member_cannot_set_role(clean_state: None, client: TestClient) -> None:
    tenant = _uniq_tenant()
    _provision_tenant(tenant)
    member = _provision_user(tenant, "member@example.com", role="member")
    other = _provision_user(tenant, "other@example.com", role="member")
    r = client.post(
        f"/users/{other}/role",
        json={"role": "admin"},
        headers=_bearer(tenant, member),
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "admin_required"
    assert _role_in_db(tenant, other) == "member"  # unchanged


def test_admin_promotes_then_demotes_member(
    clean_state: None, client: TestClient
) -> None:
    tenant = _uniq_tenant()
    _provision_tenant(tenant)
    admin = _provision_user(tenant, "admin@example.com", role="admin")
    member = _provision_user(tenant, "member@example.com", role="member")

    # Promote.
    r = client.post(
        f"/users/{member}/role",
        json={"role": "admin"},
        headers=_bearer(tenant, admin),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"user_id": member, "role": "admin"}
    assert _role_in_db(tenant, member) == "admin"

    # The newly-promoted user can now perform an admin write.
    r2 = client.post(
        f"/users/{member}/aliases",
        json={"source_api": "mcp", "source_identifier": "y@example.com"},
        headers=_bearer(tenant, member),
    )
    assert r2.status_code == 201, r2.text

    # Demote back — allowed because `admin` is still an admin.
    r3 = client.post(
        f"/users/{member}/role",
        json={"role": "member"},
        headers=_bearer(tenant, admin),
    )
    assert r3.status_code == 200, r3.text
    assert _role_in_db(tenant, member) == "member"


def test_cannot_demote_last_admin(
    clean_state: None, client: TestClient
) -> None:
    tenant = _uniq_tenant()
    _provision_tenant(tenant)
    admin = _provision_user(tenant, "solo-admin@example.com", role="admin")
    # The sole admin tries to demote themselves → blocked.
    r = client.post(
        f"/users/{admin}/role",
        json={"role": "member"},
        headers=_bearer(tenant, admin),
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "last_admin"
    assert _role_in_db(tenant, admin) == "admin"  # unchanged


def test_set_role_invalid_value(
    clean_state: None, client: TestClient
) -> None:
    tenant = _uniq_tenant()
    _provision_tenant(tenant)
    admin = _provision_user(tenant, "admin@example.com", role="admin")
    target = _provision_user(tenant, "t@example.com", role="member")
    r = client.post(
        f"/users/{target}/role",
        json={"role": "superuser"},
        headers=_bearer(tenant, admin),
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "invalid_role"


def test_set_role_unknown_user_404(
    clean_state: None, client: TestClient
) -> None:
    tenant = _uniq_tenant()
    _provision_tenant(tenant)
    admin = _provision_user(tenant, "admin@example.com", role="admin")
    r = client.post(
        f"/users/{uuid.uuid4()}/role",
        json={"role": "admin"},
        headers=_bearer(tenant, admin),
    )
    assert r.status_code == 404, r.text


def test_roster_includes_role(
    clean_state: None, client: TestClient
) -> None:
    tenant = _uniq_tenant()
    _provision_tenant(tenant)
    _provision_user(tenant, "rick@example.com", role="admin")
    # Email-matched activity auto-links on the reconcile that /users runs.
    _seed_code_analytics(tenant, email="rick@example.com")
    body = client.get("/users", headers=_bearer(tenant)).json()
    rick = [u for u in body["users"] if u["email"] == "rick@example.com"]
    assert len(rick) == 1, body["users"]
    assert rick[0]["role"] == "admin"
