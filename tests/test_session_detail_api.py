# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the T5.5 SessionDetail endpoint.

Covers: happy path, content decryption (with stub retriever),
malformed session_id → 400, cross-tenant session_id → 404,
nonexistent session_id → 404.
"""

from __future__ import annotations

import base64
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


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


@pytest.fixture
def clean_records() -> Iterator[None]:
    from vargate_telemetry.api import sessions as sessions_routes
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
    sessions_routes.set_content_retriever_for_test(None)


def _bearer_for_tenant(tenant_id: str) -> dict[str, str]:
    from vargate_telemetry.auth.jwt import issue_session_jwt

    token = issue_session_jwt(
        user_id=str(uuid.uuid4()),
        email="probe@example.com",
        sso_provider="google",
        tenant_id=tenant_id,
    )
    return {"Authorization": f"Bearer {token}"}


def _encode_session_id(date_iso: str, actor_type: str, actor_key: str) -> str:
    raw = f"{date_iso}|{actor_type}|{actor_key}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _seed_record(
    tenant_id: str,
    *,
    occurred_at: datetime,
    source_api: str,
    actor_type: str,
    actor_key: str,
    external_id: str | None = None,
    content_ref: str | None = None,
    extra_metadata: dict | None = None,
) -> None:
    from vargate_telemetry.db import engine

    md = {
        "actor": {"type": actor_type, "email_address": actor_key},
    }
    if extra_metadata:
        md.update(extra_metadata)
    eid = (
        external_id
        or f"{source_api}:{occurred_at.date().isoformat()}:{actor_key}"
    )
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata, content_ref,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :tenant_id, :record_type, :source_api, :external_id,
                    :occurred_at, decode(:content_hash_hex, 'hex'),
                    :metadata, :content_ref,
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
                "tenant_id_lookup": tenant_id,
                "record_type": source_api,
                "source_api": source_api,
                "external_id": eid,
                "occurred_at": occurred_at,
                "content_hash_hex": "00" * 32,
                "metadata": json.dumps(md),
                "content_ref": content_ref,
                "prev_hex": "00" * 32,
                "self_hex": "11" * 32,
            },
        )


# ───────────────────────────────────────────────────────────────────────────
# Happy path
# ───────────────────────────────────────────────────────────────────────────


def test_session_detail_happy_path(
    clean_records: None, client: TestClient
) -> None:
    """Seed a code_analytics record + a matching activity_feed record
    on the same date for the same user → SessionDetail returns both
    records in chain order."""
    tenant = "tnt_us_detail_happy"
    base = datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc)
    _seed_record(
        tenant,
        occurred_at=base,
        source_api="code_analytics",
        actor_type="user_actor",
        actor_key="dev@example.com",
        extra_metadata={
            "core_metrics": {"num_sessions": 5},
            "model_breakdown": [{"model": "claude-opus-4-7"}],
        },
    )
    _seed_record(
        tenant,
        occurred_at=base.replace(hour=10),
        source_api="compliance_activities",
        actor_type="user_actor",
        actor_key="dev@example.com",
        external_id="activity_01abc",
        extra_metadata={"type": "claude_chat_created"},
    )

    sid = _encode_session_id("2026-05-11", "user_actor", "dev@example.com")
    r = client.get(f"/sessions/{sid}", headers=_bearer_for_tenant(tenant))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == sid
    assert body["date"] == "2026-05-11"
    assert body["actor"] == {"type": "user_actor", "key": "dev@example.com"}
    assert len(body["records"]) == 2

    # Records are sorted by occurred_at + chain_seq; the code_analytics
    # row at hour 0 comes before the activity at hour 10.
    assert body["records"][0]["source_api"] == "code_analytics"
    assert body["records"][0]["metadata"]["core_metrics"]["num_sessions"] == 5
    assert body["records"][1]["source_api"] == "compliance_activities"
    assert body["records"][1]["metadata"]["type"] == "claude_chat_created"

    # No content_ref on either record → content is null on both.
    for rec in body["records"]:
        assert rec["content"] is None


def test_session_detail_delineates_claude_code_surface(
    clean_records: None, client: TestClient
) -> None:
    """TM4 #3 — the per-record source in the detail reflects the
    effective surface: an MCP tool_use turn renders as claude_code, a
    chat turn stays mcp, so the detail agrees with the list's
    distribution split (no more lumping Claude Code work as chat)."""
    tenant = "tnt_us_detail_surface"
    base = datetime(2026, 5, 13, 14, 0, tzinfo=timezone.utc)
    _seed_record(
        tenant,
        occurred_at=base,
        source_api="mcp",
        actor_type="user_actor",
        actor_key="dev@example.com",
        external_id="mcp_toolu_1",
        extra_metadata={"kind": "tool_use", "summary": "edited files"},
    )
    _seed_record(
        tenant,
        occurred_at=base.replace(minute=5),
        source_api="mcp",
        actor_type="user_actor",
        actor_key="dev@example.com",
        external_id="mcp_chat_1",
        extra_metadata={"kind": "chat", "summary": "asked a question"},
    )

    sid = _encode_session_id("2026-05-13", "user_actor", "dev@example.com")
    r = client.get(f"/sessions/{sid}", headers=_bearer_for_tenant(tenant))
    assert r.status_code == 200, r.text
    records = r.json()["records"]
    assert len(records) == 2
    # Ordered by occurred_at: the 14:00 tool_use turn → Claude Code,
    # then the 14:05 chat turn → Claude (chat).
    assert records[0]["source_api"] == "claude_code"
    assert records[1]["source_api"] == "mcp"


# ───────────────────────────────────────────────────────────────────────────
# Decryption — content_ref + stubbed retriever returns plaintext
# ───────────────────────────────────────────────────────────────────────────


def test_session_detail_decrypts_content_via_retriever(
    clean_records: None, client: TestClient
) -> None:
    """A record with content_ref non-null triggers the decrypt path.
    Stub the content retriever to return known bytes; assert they
    surface in the response's `content` field. T5.6 is the first
    ingest path that will populate content_ref in production; this
    test pins the wire shape so the dashboard reads cleanly when
    that lands."""
    from vargate_telemetry.api import sessions as sessions_routes

    tenant = "tnt_us_detail_decrypt"
    _seed_record(
        tenant,
        occurred_at=datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc),
        source_api="compliance_activities",
        actor_type="user_actor",
        actor_key="dev@example.com",
        external_id="activity_with_content",
        content_ref="2026/05/11/abc123.enc",
        extra_metadata={"type": "claude_chat_created"},
    )

    # Stub retriever: return a known plaintext rather than hit MinIO/HSM.
    captured: dict[str, str] = {}

    def fake_retrieve(tid: str, ref: str) -> bytes:
        captured["tenant_id"] = tid
        captured["content_ref"] = ref
        return b'{"plaintext": "this was a chat message"}'

    sessions_routes.set_content_retriever_for_test(fake_retrieve)

    sid = _encode_session_id("2026-05-11", "user_actor", "dev@example.com")
    r = client.get(f"/sessions/{sid}", headers=_bearer_for_tenant(tenant))
    assert r.status_code == 200, r.text
    body = r.json()

    assert captured == {
        "tenant_id": tenant,
        "content_ref": "2026/05/11/abc123.enc",
    }
    assert len(body["records"]) == 1
    rec = body["records"][0]
    assert rec["content"] == '{"plaintext": "this was a chat message"}'


# ───────────────────────────────────────────────────────────────────────────
# Cross-tenant 404
# ───────────────────────────────────────────────────────────────────────────


def test_session_detail_cross_tenant_returns_404(
    clean_records: None, client: TestClient
) -> None:
    """Tenant B requesting tenant A's session_id (which is decodable
    and well-formed but the underlying rows belong to a different
    tenant) gets 404 — RLS hides the rows, and the 404 doesn't leak
    that 'a session with this id exists somewhere.'"""
    tenant_a = "tnt_us_detail_xt_a"
    tenant_b = "tnt_us_detail_xt_b"
    _seed_record(
        tenant_a,
        occurred_at=datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc),
        source_api="code_analytics",
        actor_type="user_actor",
        actor_key="dev@a.com",
    )

    # Encode the session_id that's REAL for tenant_a, then call it
    # with tenant_b's bearer.
    sid = _encode_session_id("2026-05-11", "user_actor", "dev@a.com")
    r = client.get(f"/sessions/{sid}", headers=_bearer_for_tenant(tenant_b))
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "session_not_found"


# ───────────────────────────────────────────────────────────────────────────
# Nonexistent session_id → 404
# ───────────────────────────────────────────────────────────────────────────


def test_session_detail_nonexistent_returns_404(
    clean_records: None, client: TestClient
) -> None:
    """A well-formed but never-seen session_id returns 404."""
    sid = _encode_session_id(
        "2030-01-01", "user_actor", "ghost@example.com"
    )
    r = client.get(
        f"/sessions/{sid}",
        headers=_bearer_for_tenant("tnt_us_detail_404"),
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "session_not_found"


# ───────────────────────────────────────────────────────────────────────────
# Malformed session_id → 400
# ───────────────────────────────────────────────────────────────────────────


def test_session_detail_malformed_session_id_returns_400(
    client: TestClient,
) -> None:
    """A non-base64url string OR a base64url string that decodes to
    something other than `date|actor_type|actor_key` returns 400."""
    # Non-base64.
    r = client.get(
        "/sessions/not!valid!base64",
        headers=_bearer_for_tenant("tnt_us_detail_malformed"),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_session_id"

    # Base64 but decodes to garbage (no pipes).
    bad = base64.urlsafe_b64encode(b"justonepiece").rstrip(b"=").decode()
    r = client.get(
        f"/sessions/{bad}",
        headers=_bearer_for_tenant("tnt_us_detail_malformed"),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_session_id"

    # Base64 but date component isn't ISO 8601.
    bad = (
        base64.urlsafe_b64encode(b"NOT-A-DATE|user_actor|dev@x.com")
        .rstrip(b"=")
        .decode()
    )
    r = client.get(
        f"/sessions/{bad}",
        headers=_bearer_for_tenant("tnt_us_detail_malformed"),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_session_id"


def test_session_detail_rejects_no_tenant_bound(
    client: TestClient,
) -> None:
    sid = _encode_session_id("2026-05-11", "user_actor", "dev@x.com")
    from vargate_telemetry.auth.jwt import issue_session_jwt

    token = issue_session_jwt(
        user_id=str(uuid.uuid4()),
        email="probe@example.com",
        sso_provider="google",
        tenant_id=None,
    )
    r = client.get(
        f"/sessions/{sid}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "no_tenant_bound"
