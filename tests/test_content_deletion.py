# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for content deletion (TM6 T6.1) — chain-safe, tamper-evident.

Covers the service (``delete_chat`` / ``delete_user_content`` /
``crypto_shred_tenant``) and the admin-gated endpoints. Seeds REAL
content (``store_content`` → an encrypted MinIO blob + a chain-bound
``chat_message`` record pointing at it) so the load-bearing properties
are asserted against the live dev stack, not mocks:

  - the blob is actually GONE after deletion;
  - the original chain records are NEVER removed;
  - ``verify_telemetry_chain`` stays green across every deletion (the
    AGCS "prove it existed AND prove it was deleted" property).

Unique tenant per test + scoped DELETE teardown (residue-immune).
"""

from __future__ import annotations

import os
import uuid
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


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


@pytest.fixture
def make_tenant():
    """Provision tenant + DEK + a user (admin by default). Scoped teardown
    over every table the deletion paths touch (incl. telemetry_records)."""
    from vargate_telemetry.crypto.seal import provision_tenant_dek
    from vargate_telemetry.db import engine

    created: list[str] = []

    def _make(role: str = "admin") -> tuple[str, uuid.UUID]:
        sfx = uuid.uuid4().hex[:12]
        tenant_id = f"tnt_eu_del_{sfx}"
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
                    "email": f"del-{sfx}@example.com",
                    "sub": f"google-sub-{sfx}",
                    "t": tenant_id,
                    "role": role,
                },
            )
        provision_tenant_dek(tenant_id)
        # Compliance-tier tenant: seal a (fake) Compliance Access Key so
        # the content endpoints' content_capture gate passes.
        from vargate_telemetry.anthropic import ANTHROPIC_COMPLIANCE_KEY_SECRET
        from vargate_telemetry.crypto.seal import seal_secret

        seal_secret(
            tenant_id, ANTHROPIC_COMPLIANCE_KEY_SECRET, b"sk-ant-api01-testcompliance"
        )
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
    tenant_id: str,
    chat_id: str,
    msg_id: str,
    role: str,
    *,
    occurred_at: datetime,
    text: str = "secret content",
    subject_user_id: Optional[str] = None,
    chat_name: str = "Requirements chat",
    user_email: str = "user@example.com",
) -> str:
    """Store a real encrypted blob + append a chain-bound chat_message
    record pointing at it. Returns the content_ref."""
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
        subject_user_id=subject_user_id,
        record_metadata={
            "chat_id": chat_id,
            "message_id": msg_id,
            "role": role,
            "chat_name": chat_name,
            "model": "claude-opus-4-7",
            "user_email": user_email,
        },
    )
    return content_ref


def _blob_exists(tenant_id: str, content_ref: str) -> bool:
    from vargate_telemetry.storage.content import retrieve_content

    try:
        retrieve_content(tenant_id, content_ref)
        return True
    except Exception:
        return False


def _count(tenant_id: str, record_type: str) -> int:
    from vargate_telemetry.db import session_scope

    with session_scope(tenant_id) as s:
        return s.execute(
            sql_text(
                "SELECT count(*) FROM telemetry_records WHERE "
                "tenant_id = :t AND record_type = :rt"
            ),
            {"t": tenant_id, "rt": record_type},
        ).scalar_one()


# ───────────────────────────────────────────────────────────────────────────
# service — delete_chat
# ───────────────────────────────────────────────────────────────────────────


def test_delete_chat_removes_blobs_keeps_records_and_chain(make_tenant) -> None:
    from vargate_telemetry import content_deletion
    from vargate_telemetry.chain import verify_telemetry_chain

    tenant, _ = make_tenant()
    r1 = _seed_content(tenant, "chat_A", "m1", "user", occurred_at=_T)
    r2 = _seed_content(
        tenant, "chat_A", "m2", "assistant", occurred_at=_T.replace(hour=11)
    )
    assert _blob_exists(tenant, r1) and _blob_exists(tenant, r2)

    result = content_deletion.delete_chat(
        tenant, "chat_A", reason="DSR", requested_by="admin-1"
    )
    assert result == {"deleted": 2, "already_deleted": 0}

    # Blobs gone; ORIGINAL records intact; one deletion event per message.
    assert not _blob_exists(tenant, r1)
    assert not _blob_exists(tenant, r2)
    assert _count(tenant, "chat_message") == 2  # records NEVER removed
    assert _count(tenant, "content_deletion") == 2
    # The chain still verifies end-to-end (the AGCS property).
    assert verify_telemetry_chain(tenant).valid is True


def test_delete_chat_is_idempotent(make_tenant) -> None:
    from vargate_telemetry import content_deletion

    tenant, _ = make_tenant()
    _seed_content(tenant, "chat_A", "m1", "user", occurred_at=_T)
    first = content_deletion.delete_chat(
        tenant, "chat_A", reason="x", requested_by="a"
    )
    second = content_deletion.delete_chat(
        tenant, "chat_A", reason="x", requested_by="a"
    )
    assert first == {"deleted": 1, "already_deleted": 0}
    assert second == {"deleted": 0, "already_deleted": 1}
    assert _count(tenant, "content_deletion") == 1  # no duplicate event


# ───────────────────────────────────────────────────────────────────────────
# service — delete_user_content (DSR)
# ───────────────────────────────────────────────────────────────────────────


def test_delete_user_content_scopes_to_subject(make_tenant) -> None:
    from vargate_telemetry import content_deletion
    from vargate_telemetry.chain import verify_telemetry_chain

    tenant, _ = make_tenant()
    u1 = str(uuid.uuid4())
    u2 = str(uuid.uuid4())
    a = _seed_content(
        tenant, "chat_1", "m1", "user", occurred_at=_T, subject_user_id=u1
    )
    b = _seed_content(
        tenant, "chat_2", "m2", "user", occurred_at=_T, subject_user_id=u1
    )
    c = _seed_content(
        tenant, "chat_3", "m3", "user", occurred_at=_T, subject_user_id=u2
    )

    result = content_deletion.delete_user_content(
        tenant, u1, reason="GDPR erasure", requested_by="admin"
    )
    assert result == {"deleted": 2, "already_deleted": 0}
    assert not _blob_exists(tenant, a) and not _blob_exists(tenant, b)
    assert _blob_exists(tenant, c)  # the OTHER subject is untouched
    assert verify_telemetry_chain(tenant).valid is True


# ───────────────────────────────────────────────────────────────────────────
# service — crypto_shred_tenant
# ───────────────────────────────────────────────────────────────────────────


def test_crypto_shred_destroys_dek_and_records_event(make_tenant) -> None:
    from vargate_telemetry import content_deletion
    from vargate_telemetry.chain import verify_telemetry_chain
    from vargate_telemetry.crypto.seal import get_tenant_dek

    tenant, _ = make_tenant()
    ref = _seed_content(tenant, "chat_A", "m1", "user", occurred_at=_T)
    assert _blob_exists(tenant, ref)

    result = content_deletion.crypto_shred_tenant(
        tenant, reason="offboard", requested_by="admin"
    )
    assert result == {"dek_destroyed": True, "event_appended": True}

    # DEK gone → content unreadable; deletion event + records remain;
    # chain still verifies (content_hash is plaintext-derived, in clear).
    with pytest.raises(LookupError):
        get_tenant_dek(tenant)
    assert not _blob_exists(tenant, ref)
    assert _count(tenant, "content_deletion") == 1
    assert _count(tenant, "chat_message") == 1
    assert verify_telemetry_chain(tenant).valid is True


def test_crypto_shred_is_idempotent(make_tenant) -> None:
    from vargate_telemetry import content_deletion

    tenant, _ = make_tenant()
    first = content_deletion.crypto_shred_tenant(
        tenant, reason="x", requested_by="a"
    )
    second = content_deletion.crypto_shred_tenant(
        tenant, reason="x", requested_by="a"
    )
    assert first == {"dek_destroyed": True, "event_appended": True}
    assert second == {"dek_destroyed": False, "event_appended": False}
    assert _count(tenant, "content_deletion") == 1


# ───────────────────────────────────────────────────────────────────────────
# endpoints
# ───────────────────────────────────────────────────────────────────────────


def test_delete_chat_endpoint_admin(client, make_tenant) -> None:
    tenant, admin = make_tenant(role="admin")
    ref = _seed_content(tenant, "chat_A", "m1", "user", occurred_at=_T)
    resp = client.request(
        "DELETE",
        "/content/chats/chat_A",
        json={"reason": "DSR request #42"},
        headers=_bearer(admin, tenant),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "scope": "chat",
        "deleted": 1,
        "already_deleted": 0,
    }
    assert not _blob_exists(tenant, ref)


def test_delete_chat_endpoint_forbidden_for_member(client, make_tenant) -> None:
    tenant, member = make_tenant(role="member")
    _seed_content(tenant, "chat_A", "m1", "user", occurred_at=_T)
    resp = client.request(
        "DELETE",
        "/content/chats/chat_A",
        json={"reason": "x"},
        headers=_bearer(member, tenant),
    )
    assert resp.status_code == 403, resp.text


def test_delete_chat_endpoint_requires_reason(client, make_tenant) -> None:
    tenant, admin = make_tenant()
    resp = client.request(
        "DELETE",
        "/content/chats/chat_A",
        json={},
        headers=_bearer(admin, tenant),
    )
    assert resp.status_code == 422, resp.text  # reason is mandatory


def test_delete_user_endpoint_admin(client, make_tenant) -> None:
    tenant, admin = make_tenant()
    u1 = str(uuid.uuid4())
    _seed_content(
        tenant, "chat_1", "m1", "user", occurred_at=_T, subject_user_id=u1
    )
    resp = client.request(
        "DELETE",
        f"/content/users/{u1}",
        json={"reason": "right to be forgotten"},
        headers=_bearer(admin, tenant),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "user"
    assert body["deleted"] == 1


def test_shred_endpoint_confirm_mismatch_400(client, make_tenant) -> None:
    tenant, admin = make_tenant()
    resp = client.post(
        "/content/tenant/shred",
        json={"reason": "offboard", "confirm_tenant_id": "tnt_wrong"},
        headers=_bearer(admin, tenant),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "confirm_mismatch"


def test_shred_endpoint_confirmed_destroys(client, make_tenant) -> None:
    from vargate_telemetry.crypto.seal import get_tenant_dek

    tenant, admin = make_tenant()
    _seed_content(tenant, "chat_A", "m1", "user", occurred_at=_T)
    resp = client.post(
        "/content/tenant/shred",
        json={"reason": "offboard", "confirm_tenant_id": tenant},
        headers=_bearer(admin, tenant),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["dek_destroyed"] is True
    with pytest.raises(LookupError):
        get_tenant_dek(tenant)


# ───────────────────────────────────────────────────────────────────────────
# view reflects purge (tombstones)
# ───────────────────────────────────────────────────────────────────────────


def test_view_reflects_chat_purge(client, make_tenant) -> None:
    from vargate_telemetry import content_deletion

    tenant, admin = make_tenant()
    _seed_content(tenant, "chat_A", "m1", "user", occurred_at=_T)
    _seed_content(
        tenant, "chat_A", "m2", "assistant", occurred_at=_T.replace(hour=11)
    )
    content_deletion.delete_chat(
        tenant, "chat_A", reason="DSR #9", requested_by="admin"
    )

    # List: the chat still appears ONCE (deletion events don't pollute the
    # aggregation), now flagged purged with the right message_count.
    lst = client.get("/content/chats", headers=_bearer(admin, tenant))
    assert lst.status_code == 200, lst.text
    chats = lst.json()["chats"]
    assert len(chats) == 1
    assert chats[0]["chat_id"] == "chat_A"
    assert chats[0]["message_count"] == 2  # NOT 4 (events excluded)
    assert chats[0]["purged"] is True

    # Detail: messages tombstoned (content null + purged), chat-level purge
    # carries the reason.
    det = client.get("/content/chats/chat_A", headers=_bearer(admin, tenant))
    assert det.status_code == 200, det.text
    body = det.json()
    assert body["purged"] is True
    assert body["purge_reason"] == "DSR #9"
    assert body["purged_at"] is not None
    assert all(
        m["purged"] is True and m["content"] is None for m in body["messages"]
    )


def test_view_shows_tenant_shred_tombstone(client, make_tenant) -> None:
    from vargate_telemetry import content_deletion

    tenant, admin = make_tenant()
    _seed_content(tenant, "chat_A", "m1", "user", occurred_at=_T)
    content_deletion.crypto_shred_tenant(
        tenant, reason="account closed", requested_by="admin"
    )

    det = client.get("/content/chats/chat_A", headers=_bearer(admin, tenant))
    assert det.status_code == 200, det.text
    body = det.json()
    assert body["purged"] is True
    assert all(
        m["purged"] is True and m["content"] is None for m in body["messages"]
    )


def test_partial_user_delete_keeps_chat_unpurged_in_both_views(
    client, make_tenant
) -> None:
    """A per-user DSR that purges only SOME messages of a chat must leave
    the chat 'not fully purged' — and the list + detail views must AGREE
    (regression for the list-vs-detail definition mismatch)."""
    from vargate_telemetry import content_deletion

    tenant, admin = make_tenant()
    u1 = str(uuid.uuid4())
    u2 = str(uuid.uuid4())
    # One chat, two messages from DIFFERENT subjects.
    _seed_content(
        tenant, "chat_M", "m1", "user",
        occurred_at=_T, subject_user_id=u1, text="from u1",
    )
    _seed_content(
        tenant, "chat_M", "m2", "user",
        occurred_at=_T.replace(hour=11), subject_user_id=u2, text="from u2",
    )

    content_deletion.delete_user_content(
        tenant, u1, reason="DSR u1", requested_by="admin"
    )

    # List: chat is NOT fully purged (m2 survives) — same verdict as detail.
    chats = client.get(
        "/content/chats", headers=_bearer(admin, tenant)
    ).json()["chats"]
    chat = next(c for c in chats if c["chat_id"] == "chat_M")
    assert chat["purged"] is False
    assert chat["message_count"] == 2  # both messages still counted

    # Detail: m1 tombstoned (content null), m2 still readable; chat not purged.
    det = client.get(
        "/content/chats/chat_M", headers=_bearer(admin, tenant)
    ).json()
    assert det["purged"] is False
    by_id = {m["message_id"]: m for m in det["messages"]}
    assert by_id["m1"]["purged"] is True and by_id["m1"]["content"] is None
    assert by_id["m2"]["purged"] is False and by_id["m2"]["content"] == "from u2"


def test_delete_user_endpoint_forbidden_for_member(client, make_tenant) -> None:
    tenant, member = make_tenant(role="member")
    resp = client.request(
        "DELETE",
        f"/content/users/{uuid.uuid4()}",
        json={"reason": "x"},
        headers=_bearer(member, tenant),
    )
    assert resp.status_code == 403, resp.text


def test_shred_endpoint_forbidden_for_member(client, make_tenant) -> None:
    tenant, member = make_tenant(role="member")
    resp = client.post(
        "/content/tenant/shred",
        json={"reason": "x", "confirm_tenant_id": tenant},
        headers=_bearer(member, tenant),
    )
    assert resp.status_code == 403, resp.text
