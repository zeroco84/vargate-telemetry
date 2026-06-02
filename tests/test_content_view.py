# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the read-only compliance content view (TM5 T5.3).

Seeds synthetic ``compliance_content`` telemetry records (via the real
chain append) and stubs the content retriever so the decrypt-on-read
path is exercised without live MinIO + HSM. Residue-immune: a unique
tenant per test + scoped DELETE teardown.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Iterator, Optional

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

import os

os.environ.setdefault(
    "JWT_SIGNING_KEY",
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b",
)


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_retriever() -> Iterator[None]:
    from vargate_telemetry.api.content import set_content_retriever_for_test

    set_content_retriever_for_test(None)
    yield
    set_content_retriever_for_test(None)


@pytest.fixture
def content_tenant() -> Iterator[str]:
    """Unique tenant for content-view tests; scoped DELETE teardown."""
    from vargate_telemetry.db import engine

    tid = f"tnt_eu_cv_{uuid.uuid4().hex[:12]}"
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "billing_status) VALUES (:t, 'eu', true, 'paying')"
            ),
            {"t": tid},
        )
    yield tid
    with engine.begin() as conn:
        for tbl in ("telemetry_records", "tenants"):
            conn.execute(
                sql_text(f"DELETE FROM {tbl} WHERE tenant_id = :t"),
                {"t": tid},
            )


def _bearer(tenant_id: Optional[str]) -> dict:
    from vargate_telemetry.auth.jwt import issue_session_jwt

    token = issue_session_jwt(
        user_id=str(uuid.uuid4()),
        email="viewer@example.com",
        sso_provider="google",
        tenant_id=tenant_id,
    )
    return {"Authorization": f"Bearer {token}"}


def _seed_message(
    tenant_id: str,
    chat_id: str,
    msg_id: str,
    role: str,
    *,
    occurred_at: datetime,
    content_ref: str,
    text: str = "hello",
    chat_name: str = "Requirements chat",
    model: str = "claude-opus-4-7",
    user_email: str = "user@example.com",
    deleted_at: Optional[str] = None,
) -> None:
    from vargate_telemetry.chain import append_telemetry_record

    md = {
        "chat_id": chat_id,
        "message_id": msg_id,
        "role": role,
        "chat_name": chat_name,
        "model": model,
        "user_email": user_email,
    }
    if deleted_at is not None:
        md["chat_deleted_at"] = deleted_at
    append_telemetry_record(
        tenant_id,
        record_type="chat_message",
        source_api="compliance_content",
        external_id=msg_id,
        occurred_at=occurred_at,
        content_hash=hashlib.sha256(text.encode("utf-8")).digest(),
        content_ref=content_ref,
        content_size_bytes=len(text),
        record_metadata=md,
    )


_T = datetime(2026, 5, 20, 10, 0, 0, tzinfo=timezone.utc)


# ───────────────────────────────────────────────────────────────────────────
# GET /content/chats
# ───────────────────────────────────────────────────────────────────────────


def test_list_chats_empty(client: TestClient, content_tenant: str) -> None:
    resp = client.get("/content/chats", headers=_bearer(content_tenant))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"chats": [], "truncated": False}


def test_list_chats_aggregates_by_chat(
    client: TestClient, content_tenant: str
) -> None:
    # Chat A: 2 messages; Chat B: 1 message (more recent).
    _seed_message(
        content_tenant, "chat_A", "msg_A1", "user",
        occurred_at=_T, content_ref="r/a1",
    )
    _seed_message(
        content_tenant, "chat_A", "msg_A2", "assistant",
        occurred_at=_T.replace(hour=11), content_ref="r/a2",
    )
    _seed_message(
        content_tenant, "chat_B", "msg_B1", "user",
        occurred_at=_T.replace(day=21), content_ref="r/b1",
        chat_name="Launch plan", deleted_at="2026-05-22T09:00:00Z",
    )

    resp = client.get("/content/chats", headers=_bearer(content_tenant))
    assert resp.status_code == 200, resp.text
    chats = resp.json()["chats"]
    assert [c["chat_id"] for c in chats] == ["chat_B", "chat_A"]  # newest first

    by_id = {c["chat_id"]: c for c in chats}
    assert by_id["chat_A"]["message_count"] == 2
    assert by_id["chat_A"]["chat_name"] == "Requirements chat"
    assert by_id["chat_A"]["deleted"] is False
    assert by_id["chat_B"]["message_count"] == 1
    assert by_id["chat_B"]["chat_name"] == "Launch plan"
    assert by_id["chat_B"]["deleted"] is True  # soft-deleted flag


def test_list_chats_isolated_per_tenant(
    client: TestClient, content_tenant: str
) -> None:
    """A different tenant's bearer sees none of this tenant's chats (RLS)."""
    _seed_message(
        content_tenant, "chat_secret", "msg_x", "user",
        occurred_at=_T, content_ref="r/x",
    )
    other = f"tnt_eu_cv_{uuid.uuid4().hex[:12]}"
    resp = client.get("/content/chats", headers=_bearer(other))
    assert resp.status_code == 200, resp.text
    assert resp.json()["chats"] == []


def test_list_chats_no_tenant_bound_is_400(client: TestClient) -> None:
    resp = client.get("/content/chats", headers=_bearer(None))
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "no_tenant_bound"


# ───────────────────────────────────────────────────────────────────────────
# GET /content/chats/{chat_id}
# ───────────────────────────────────────────────────────────────────────────


def _install_retriever(mapping: dict) -> None:
    from vargate_telemetry.api.content import set_content_retriever_for_test

    set_content_retriever_for_test(
        lambda _t, ref: mapping[ref].encode("utf-8")
    )


def test_chat_detail_decrypts_messages_in_order(
    client: TestClient, content_tenant: str
) -> None:
    _seed_message(
        content_tenant, "chat_C", "msg_C1", "user",
        occurred_at=_T, content_ref="r/c1", text="Draft the requirements?",
    )
    _seed_message(
        content_tenant, "chat_C", "msg_C2", "assistant",
        occurred_at=_T.replace(hour=11), content_ref="r/c2",
        text="Here is a draft.",
    )
    _install_retriever(
        {"r/c1": "Draft the requirements?", "r/c2": "Here is a draft."}
    )

    resp = client.get(
        "/content/chats/chat_C", headers=_bearer(content_tenant)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chat_id"] == "chat_C"
    assert body["chat_name"] == "Requirements chat"
    assert body["model"] == "claude-opus-4-7"
    msgs = body["messages"]
    assert [m["message_id"] for m in msgs] == ["msg_C1", "msg_C2"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "Draft the requirements?"
    assert msgs[1]["content"] == "Here is a draft."


def test_chat_detail_unknown_is_404(
    client: TestClient, content_tenant: str
) -> None:
    resp = client.get(
        "/content/chats/does_not_exist", headers=_bearer(content_tenant)
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "chat_not_found"


def test_chat_detail_decrypt_failure_yields_null_content(
    client: TestClient, content_tenant: str
) -> None:
    """A decrypt failure (tamper / missing blob) surfaces as null content
    — the message still renders, the request doesn't 500."""
    from vargate_telemetry.api.content import set_content_retriever_for_test

    _seed_message(
        content_tenant, "chat_D", "msg_D1", "user",
        occurred_at=_T, content_ref="r/d1",
    )

    def _boom(_t: str, _ref: str) -> bytes:
        raise RuntimeError("integrity check failed")

    set_content_retriever_for_test(_boom)

    resp = client.get(
        "/content/chats/chat_D", headers=_bearer(content_tenant)
    )
    assert resp.status_code == 200, resp.text
    msgs = resp.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["content"] is None  # decrypt failed -> null, not 500


def test_chat_detail_soft_deleted_flag(
    client: TestClient, content_tenant: str
) -> None:
    _seed_message(
        content_tenant, "chat_E", "msg_E1", "user",
        occurred_at=_T, content_ref="r/e1",
        deleted_at="2026-05-21T09:00:00Z",
    )
    _install_retriever({"r/e1": "in a deleted chat"})

    resp = client.get(
        "/content/chats/chat_E", headers=_bearer(content_tenant)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] is True


def test_chat_detail_isolated_per_tenant(
    client: TestClient, content_tenant: str
) -> None:
    """Another tenant cannot read this tenant's chat by guessing the id."""
    _seed_message(
        content_tenant, "chat_shared_id", "msg_z", "user",
        occurred_at=_T, content_ref="r/z",
    )
    other = f"tnt_eu_cv_{uuid.uuid4().hex[:12]}"
    resp = client.get(
        "/content/chats/chat_shared_id", headers=_bearer(other)
    )
    assert resp.status_code == 404, resp.text
