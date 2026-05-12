# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the T5.5 Sessions list endpoint.

Covers: happy path, pagination cursor, RLS isolation, source_api
filter, actor filter, date range filter, empty state, no-tenant-bound
guard.

Session-detail tests live in ``test_session_detail_api.py``.

All tests seed synthetic telemetry_records via direct INSERT and run
the route through FastAPI's TestClient. RLS scoping is exercised by
seeding two tenants' rows and asserting the requesting tenant can
only see its own.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

os.environ.setdefault(
    "JWT_SIGNING_KEY",
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b",
)


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


@pytest.fixture
def clean_records() -> Iterator[None]:
    """Empty telemetry_records before AND after each test so RLS-isolation
    cross-checks don't see residue from a prior test."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE")
        )
    yield
    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE")
        )


def _bearer_for_tenant(tenant_id: str | None) -> dict[str, str]:
    """Bearer JWT for a synthetic user bound to ``tenant_id``."""
    from vargate_telemetry.auth.jwt import issue_session_jwt

    token = issue_session_jwt(
        user_id=str(uuid.uuid4()),
        email="probe@example.com",
        sso_provider="google",
        tenant_id=tenant_id,
    )
    return {"Authorization": f"Bearer {token}"}


def _seed_record(
    tenant_id: str,
    *,
    occurred_at: datetime,
    source_api: str,
    actor_type: str,
    actor_key_field: str,
    actor_key: str,
    external_id: str | None = None,
    extra_metadata: dict | None = None,
) -> None:
    """Insert one telemetry_records row directly via SQL. Bypasses the
    audit chain (T5.5 list endpoint doesn't read chain state — only
    metadata + indexed columns)."""
    from vargate_telemetry.db import engine

    md = {
        "actor": {"type": actor_type, actor_key_field: actor_key},
    }
    if extra_metadata:
        md.update(extra_metadata)
    eid = external_id or f"{source_api}:{occurred_at.date().isoformat()}:{actor_key}"
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :tenant_id, :record_type, :source_api, :external_id,
                    :occurred_at, decode(:content_hash_hex, 'hex'),
                    :metadata,
                    (SELECT COALESCE(MAX(chain_seq), 0) + 1
                       FROM telemetry_records
                      WHERE tenant_id = :tenant_id_lookup),
                    decode(:prev_hex, 'hex'),
                    decode(:self_hex, 'hex')
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                # Same value, separate parameter name so psycopg can
                # type-infer each independently (the INSERT column
                # forces tenant_id → varchar from the schema; the
                # subquery WHERE has no schema hint and would clash
                # if it shared the parameter).
                "tenant_id_lookup": tenant_id,
                "record_type": source_api,
                "source_api": source_api,
                "external_id": eid,
                "occurred_at": occurred_at,
                "content_hash_hex": "00" * 32,
                "metadata": json.dumps(md),
                # Stub chain values — sessions endpoint doesn't read
                # them, so we don't need real chain math for these tests.
                "prev_hex": "00" * 32,
                "self_hex": "11" * 32,
            },
        )


# ───────────────────────────────────────────────────────────────────────────
# Happy path — list returns code_analytics sessions
# ───────────────────────────────────────────────────────────────────────────


def test_list_sessions_happy_path(
    clean_records: None, client: TestClient
) -> None:
    """Seed two code_analytics records for one tenant → list returns
    two sessions, one per (date, actor)."""
    tenant = "tnt_us_test_happy"
    # Two distinct actors on the same date → two sessions.
    _seed_record(
        tenant,
        occurred_at=datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc),
        source_api="code_analytics",
        actor_type="user_actor",
        actor_key_field="email_address",
        actor_key="alice@example.com",
    )
    _seed_record(
        tenant,
        occurred_at=datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc),
        source_api="code_analytics",
        actor_type="user_actor",
        actor_key_field="email_address",
        actor_key="bob@example.com",
    )

    r = client.get("/sessions", headers=_bearer_for_tenant(tenant))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["sessions"]) == 2
    assert body["next_cursor"] is None

    actor_keys = sorted(s["actor"]["key"] for s in body["sessions"])
    assert actor_keys == ["alice@example.com", "bob@example.com"]

    for s in body["sessions"]:
        assert s["actor"]["type"] == "user_actor"
        assert s["source_apis"] == ["code_analytics"]
        assert s["event_count"] == 1
        assert s["date"] == "2026-05-11"
        assert isinstance(s["session_id"], str) and len(s["session_id"]) > 0


def test_list_sessions_aggregates_activity_feed_into_same_session(
    clean_records: None, client: TestClient
) -> None:
    """A code_analytics record + matching activity_feed records on the
    same date for the same user-actor → ONE session, event_count
    reflects both streams, source_apis lists both."""
    tenant = "tnt_us_test_agg"
    base = datetime(2026, 5, 11, 8, 0, tzinfo=timezone.utc)
    _seed_record(
        tenant,
        occurred_at=base,
        source_api="code_analytics",
        actor_type="user_actor",
        actor_key_field="email_address",
        actor_key="dev@example.com",
    )
    _seed_record(
        tenant,
        occurred_at=base.replace(hour=10),
        source_api="compliance_activities",
        actor_type="user_actor",
        actor_key_field="email_address",
        actor_key="dev@example.com",
        external_id="activity_01abc",
        extra_metadata={"type": "claude_chat_created"},
    )
    _seed_record(
        tenant,
        occurred_at=base.replace(hour=14),
        source_api="compliance_activities",
        actor_type="user_actor",
        actor_key_field="email_address",
        actor_key="dev@example.com",
        external_id="activity_01def",
        extra_metadata={"type": "claude_file_uploaded"},
    )

    r = client.get("/sessions", headers=_bearer_for_tenant(tenant))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["sessions"]) == 1
    s = body["sessions"][0]
    assert s["event_count"] == 3
    # Sorted by SQL ARRAY_AGG DISTINCT ORDER BY source_api ASC.
    assert s["source_apis"] == ["code_analytics", "compliance_activities"]


def test_list_sessions_excludes_admin_usage_records(
    clean_records: None, client: TestClient
) -> None:
    """Admin API usage records are bucket-grain (no actor field), so
    they're explicitly excluded from Sessions. Seeding an admin record
    + a code_analytics record → only the code_analytics one shows up."""
    tenant = "tnt_us_test_no_admin"
    _seed_record(
        tenant,
        occurred_at=datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc),
        source_api="admin",
        actor_type="user_actor",  # admin records often DON'T have
        actor_key_field="email_address",  # actor; this is a stretch
        actor_key="aliase@example.com",   # but exercises the source_api filter.
    )
    _seed_record(
        tenant,
        occurred_at=datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc),
        source_api="code_analytics",
        actor_type="user_actor",
        actor_key_field="email_address",
        actor_key="alice@example.com",
    )

    r = client.get("/sessions", headers=_bearer_for_tenant(tenant))
    body = r.json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["actor"]["key"] == "alice@example.com"


# ───────────────────────────────────────────────────────────────────────────
# RLS isolation — tenant A can't see tenant B's sessions
# ───────────────────────────────────────────────────────────────────────────


def test_list_sessions_rls_isolation(
    clean_records: None, client: TestClient
) -> None:
    """Seed code_analytics records under TWO tenants. Request from
    tenant A: see only tenant A's sessions. Request from tenant B:
    see only tenant B's. The RLS policy on telemetry_records enforces
    this at the DB level — the route doesn't filter by tenant_id in
    its WHERE clause, only via `current_setting('app.tenant_id')`."""
    tenant_a = "tnt_us_isolation_a"
    tenant_b = "tnt_us_isolation_b"

    _seed_record(
        tenant_a,
        occurred_at=datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc),
        source_api="code_analytics",
        actor_type="user_actor",
        actor_key_field="email_address",
        actor_key="alice@a.com",
    )
    _seed_record(
        tenant_b,
        occurred_at=datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc),
        source_api="code_analytics",
        actor_type="user_actor",
        actor_key_field="email_address",
        actor_key="alice@b.com",  # same actor name, different tenants
    )

    # Tenant A's view.
    r_a = client.get("/sessions", headers=_bearer_for_tenant(tenant_a))
    body_a = r_a.json()
    assert len(body_a["sessions"]) == 1
    assert body_a["sessions"][0]["actor"]["key"] == "alice@a.com"

    # Tenant B's view.
    r_b = client.get("/sessions", headers=_bearer_for_tenant(tenant_b))
    body_b = r_b.json()
    assert len(body_b["sessions"]) == 1
    assert body_b["sessions"][0]["actor"]["key"] == "alice@b.com"


# ───────────────────────────────────────────────────────────────────────────
# Pagination — cursor advances correctly
# ───────────────────────────────────────────────────────────────────────────


def test_list_sessions_pagination_advances_via_cursor(
    clean_records: None, client: TestClient
) -> None:
    """Seed 3 sessions, request with limit=2 → first page has 2 +
    next_cursor; second page (using cursor) has the 3rd + next_cursor
    null."""
    tenant = "tnt_us_pagination"
    # Three sessions across three days (newest-first ordering).
    for day, name in [
        (11, "carol"),
        (12, "bob"),
        (13, "alice"),  # newest
    ]:
        _seed_record(
            tenant,
            occurred_at=datetime(2026, 5, day, 0, 0, tzinfo=timezone.utc),
            source_api="code_analytics",
            actor_type="user_actor",
            actor_key_field="email_address",
            actor_key=f"{name}@example.com",
        )

    # Page 1: 2 sessions, newest-first → alice, bob.
    r1 = client.get(
        "/sessions",
        params={"limit": 2},
        headers=_bearer_for_tenant(tenant),
    )
    body1 = r1.json()
    assert len(body1["sessions"]) == 2
    assert [s["actor"]["key"] for s in body1["sessions"]] == [
        "alice@example.com",
        "bob@example.com",
    ]
    assert body1["next_cursor"] is not None

    # Page 2 via cursor: 1 session (carol), no more.
    r2 = client.get(
        "/sessions",
        params={"limit": 2, "cursor": body1["next_cursor"]},
        headers=_bearer_for_tenant(tenant),
    )
    body2 = r2.json()
    assert len(body2["sessions"]) == 1
    assert body2["sessions"][0]["actor"]["key"] == "carol@example.com"
    assert body2["next_cursor"] is None


# ───────────────────────────────────────────────────────────────────────────
# Filters
# ───────────────────────────────────────────────────────────────────────────


def test_list_sessions_source_api_filter(
    clean_records: None, client: TestClient
) -> None:
    """source_api filter restricts the result to one stream's
    contribution. Note: filtering DOESN'T re-aggregate — a session
    whose Activity Feed records contributed will still appear with
    source_apis=['compliance_activities'] when filtered, but only
    rows from that stream are counted."""
    tenant = "tnt_us_source_filter"
    base = datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc)
    _seed_record(
        tenant,
        occurred_at=base,
        source_api="code_analytics",
        actor_type="user_actor",
        actor_key_field="email_address",
        actor_key="user-with-both@example.com",
    )
    _seed_record(
        tenant,
        occurred_at=base.replace(hour=14),
        source_api="compliance_activities",
        actor_type="user_actor",
        actor_key_field="email_address",
        actor_key="user-with-both@example.com",
        external_id="activity_01a",
    )
    _seed_record(
        tenant,
        occurred_at=base,
        source_api="code_analytics",
        actor_type="user_actor",
        actor_key_field="email_address",
        actor_key="user-code-only@example.com",
    )

    # Filter to code_analytics → both sessions (both have code_analytics rows).
    r = client.get(
        "/sessions",
        params={"source_api": "code_analytics"},
        headers=_bearer_for_tenant(tenant),
    )
    body = r.json()
    assert len(body["sessions"]) == 2

    # Filter to compliance_activities → only the user with activity records.
    r = client.get(
        "/sessions",
        params={"source_api": "compliance_activities"},
        headers=_bearer_for_tenant(tenant),
    )
    body = r.json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["actor"]["key"] == "user-with-both@example.com"


def test_list_sessions_actor_key_filter(
    clean_records: None, client: TestClient
) -> None:
    """actor_key filter narrows to one principal across all dates."""
    tenant = "tnt_us_actor_filter"
    for day, name in [(11, "alice"), (11, "bob"), (12, "alice")]:
        _seed_record(
            tenant,
            occurred_at=datetime(2026, 5, day, 0, 0, tzinfo=timezone.utc),
            source_api="code_analytics",
            actor_type="user_actor",
            actor_key_field="email_address",
            actor_key=f"{name}@example.com",
        )

    r = client.get(
        "/sessions",
        params={"actor_key": "alice@example.com"},
        headers=_bearer_for_tenant(tenant),
    )
    body = r.json()
    assert len(body["sessions"]) == 2
    assert {s["date"] for s in body["sessions"]} == {"2026-05-11", "2026-05-12"}
    for s in body["sessions"]:
        assert s["actor"]["key"] == "alice@example.com"


def test_list_sessions_date_range_filter(
    clean_records: None, client: TestClient
) -> None:
    """since + until are inclusive UTC date bounds. since=2026-05-11,
    until=2026-05-11 captures exactly that day."""
    tenant = "tnt_us_date_filter"
    for day in (10, 11, 12, 13):
        _seed_record(
            tenant,
            occurred_at=datetime(2026, 5, day, 0, 0, tzinfo=timezone.utc),
            source_api="code_analytics",
            actor_type="user_actor",
            actor_key_field="email_address",
            actor_key="alice@example.com",
        )

    r = client.get(
        "/sessions",
        params={"since": "2026-05-11", "until": "2026-05-12"},
        headers=_bearer_for_tenant(tenant),
    )
    body = r.json()
    assert len(body["sessions"]) == 2
    assert sorted(s["date"] for s in body["sessions"]) == [
        "2026-05-11",
        "2026-05-12",
    ]


# ───────────────────────────────────────────────────────────────────────────
# Empty state
# ───────────────────────────────────────────────────────────────────────────


def test_list_sessions_empty_state(
    clean_records: None, client: TestClient
) -> None:
    """Tenant with no records (or no Sessions-eligible records) →
    empty sessions array, null next_cursor. The frontend renders the
    'no sessions yet' state."""
    r = client.get(
        "/sessions",
        headers=_bearer_for_tenant("tnt_us_empty"),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"sessions": [], "next_cursor": None}


def test_list_sessions_rejects_no_tenant_bound(client: TestClient) -> None:
    """A JWT with tenant_id=None (post-SSO, pre-select-region) must
    400 — there's no tenant to scope sessions to."""
    r = client.get("/sessions", headers=_bearer_for_tenant(None))
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "no_tenant_bound"


def test_list_sessions_rejects_invalid_source_api(
    client: TestClient,
) -> None:
    """source_api filter must be in the eligible set."""
    r = client.get(
        "/sessions",
        params={"source_api": "admin"},  # bucket-grain, not Sessions-eligible
        headers=_bearer_for_tenant("tnt_us_invalid"),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_source_api"
