# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Redaction integration tests (TM6 T6.3) — content view masks PII by
default; the admin reveal endpoint returns unmasked content + logs a
content_reveal event; the export redacts by default and full content is
an explicit, logged option. Real blobs + a real chain."""

from __future__ import annotations

import io
import json
import os
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

os.environ.setdefault(
    "JWT_SIGNING_KEY",
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b",
)

_T = datetime(2026, 5, 20, 10, 0, 0, tzinfo=timezone.utc)
_GEN = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


@pytest.fixture
def make_tenant():
    from vargate_telemetry.crypto.seal import provision_tenant_dek
    from vargate_telemetry.db import engine

    created: list[str] = []

    def _make(role: str = "admin") -> tuple[str, uuid.UUID]:
        sfx = uuid.uuid4().hex[:12]
        tenant_id = f"tnt_eu_red_{sfx}"
        user_uuid = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                sql_text(
                    "INSERT INTO tenants (tenant_id, region, active, "
                    "billing_status) VALUES (:t, 'eu', true, 'paying')"
                ),
                {"t": tenant_id},
            )
            conn.execute(
                sql_text(
                    "INSERT INTO users (id, email, sso_provider, "
                    "sso_subject_id, name, tenant_id, role) VALUES "
                    "(:id, :email, 'google', :sub, 'Tester', :t, :role)"
                ),
                {
                    "id": str(user_uuid),
                    "email": f"red-{sfx}@example.com",
                    "sub": f"google-sub-{sfx}",
                    "t": tenant_id,
                    "role": role,
                },
            )
        provision_tenant_dek(tenant_id)
        created.append(tenant_id)
        return tenant_id, user_uuid

    yield _make

    with engine.begin() as conn:
        for table in (
            "encrypted_secrets",
            "tenant_deks",
            "telemetry_records",
            "users",
            "tenants",
        ):
            conn.execute(
                sql_text(f"DELETE FROM {table} WHERE tenant_id = ANY(:ids)"),
                {"ids": created},
            )


def _bearer(user_uuid: uuid.UUID, tenant_id: Optional[str]) -> dict:
    from vargate_telemetry.auth.jwt import issue_session_jwt

    token = issue_session_jwt(
        user_id=str(user_uuid),
        email="tester@example.com",
        sso_provider="google",
        tenant_id=tenant_id,
    )
    return {"Authorization": f"Bearer {token}"}


def _seed_content(
    tenant_id: str, chat_id: str, msg_id: str, *, occurred_at: datetime, text: str
) -> None:
    from vargate_telemetry.chain import append_telemetry_record
    from vargate_telemetry.storage.content import store_content

    content_ref, content_hash, size = store_content(tenant_id, text.encode())
    append_telemetry_record(
        tenant_id,
        record_type="chat_message",
        source_api="compliance_content",
        external_id=msg_id,
        occurred_at=occurred_at,
        content_hash=content_hash,
        content_ref=content_ref,
        content_size_bytes=size,
        record_metadata={
            "chat_id": chat_id,
            "message_id": msg_id,
            "role": "user",
            "chat_name": "Chat",
            "model": "claude-opus-4-7",
            "user_email": "user@example.com",
        },
    )


def _count_reveals(tenant_id: str) -> int:
    from vargate_telemetry.db import session_scope

    with session_scope(tenant_id) as s:
        return s.execute(
            sql_text(
                "SELECT count(*) FROM telemetry_records WHERE "
                "tenant_id = :t AND record_type = 'content_reveal'"
            ),
            {"t": tenant_id},
        ).scalar_one()


def _unzip(payload: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        return {n: zf.read(n).decode("utf-8") for n in zf.namelist()}


# ───────────────────────────────────────────────────────────────────────────
# content view — mask by default
# ───────────────────────────────────────────────────────────────────────────


def test_detail_masks_pii_by_default(client, make_tenant) -> None:
    tenant, admin = make_tenant()
    _seed_content(
        tenant, "chat_A", "m1",
        occurred_at=_T, text="Email alice@example.com please",
    )
    resp = client.get("/content/chats/chat_A", headers=_bearer(admin, tenant))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revealed"] is False
    msg = body["messages"][0]
    assert "alice@example.com" not in msg["content"]
    assert "[redacted:email]" in msg["content"]
    assert msg["redacted"] is True
    assert {"type": "email", "count": 1} in msg["redactions"]


def test_detail_no_pii_is_unchanged(client, make_tenant) -> None:
    tenant, admin = make_tenant()
    _seed_content(
        tenant, "chat_A", "m1", occurred_at=_T, text="Draft the launch plan"
    )
    body = client.get(
        "/content/chats/chat_A", headers=_bearer(admin, tenant)
    ).json()
    msg = body["messages"][0]
    assert msg["content"] == "Draft the launch plan"
    assert msg["redacted"] is False
    assert msg["redactions"] == []


# ───────────────────────────────────────────────────────────────────────────
# reveal — unmask + audit-log
# ───────────────────────────────────────────────────────────────────────────


def test_reveal_returns_unmasked_and_logs_event(client, make_tenant) -> None:
    tenant, admin = make_tenant()
    _seed_content(
        tenant, "chat_A", "m1", occurred_at=_T, text="Email alice@example.com"
    )
    assert _count_reveals(tenant) == 0

    resp = client.post(
        "/content/chats/chat_A/reveal", headers=_bearer(admin, tenant)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revealed"] is True
    msg = body["messages"][0]
    assert msg["content"] == "Email alice@example.com"  # unmasked
    assert msg["redacted"] is False
    assert {"type": "email", "count": 1} in msg["redactions"]  # still reported
    assert _count_reveals(tenant) == 1  # the reveal was audit-logged


def test_reveal_forbidden_for_member_logs_nothing(client, make_tenant) -> None:
    tenant, member = make_tenant(role="member")
    _seed_content(tenant, "chat_A", "m1", occurred_at=_T, text="x@y.com")
    resp = client.post(
        "/content/chats/chat_A/reveal", headers=_bearer(member, tenant)
    )
    assert resp.status_code == 403, resp.text
    assert _count_reveals(tenant) == 0


def test_reveal_unknown_chat_404_logs_nothing(client, make_tenant) -> None:
    tenant, admin = make_tenant()
    resp = client.post(
        "/content/chats/does_not_exist/reveal", headers=_bearer(admin, tenant)
    )
    assert resp.status_code == 404, resp.text
    assert _count_reveals(tenant) == 0  # built first → 404 before any log


# ───────────────────────────────────────────────────────────────────────────
# export — redact by default, full content is logged
# ───────────────────────────────────────────────────────────────────────────


def test_export_redacts_by_default(make_tenant) -> None:
    from vargate_telemetry import content_export

    tenant, _ = make_tenant()
    _seed_content(tenant, "chat_A", "m1", occurred_at=_T, text="ssn 123-45-6789")
    _, payload = content_export.build_export_bundle(tenant, generated_at=_GEN)
    bundle = _unzip(payload)
    assert json.loads(bundle["manifest.json"])["redacted"] is True
    msg = json.loads(bundle["chats.json"])["chats"][0]["messages"][0]
    assert "123-45-6789" not in msg["content"]
    assert msg["redacted"] is True


def test_export_reveal_full_content_and_logs(client, make_tenant) -> None:
    tenant, admin = make_tenant()
    _seed_content(tenant, "chat_A", "m1", occurred_at=_T, text="ssn 123-45-6789")
    assert _count_reveals(tenant) == 0

    resp = client.get(
        "/content/export?reveal=true", headers=_bearer(admin, tenant)
    )
    assert resp.status_code == 200, resp.text
    bundle = _unzip(resp.content)
    assert json.loads(bundle["manifest.json"])["redacted"] is False
    msg = json.loads(bundle["chats.json"])["chats"][0]["messages"][0]
    assert "123-45-6789" in msg["content"]  # full content
    assert _count_reveals(tenant) == 1  # logged
