# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the eDiscovery content export (TM6 T6.2).

Seeds REAL content (store_content → encrypted blob + chain record) so the
export's central promise is asserted end-to-end: the exported plaintext,
SHA-256'd, equals the ``content_hash`` carried in the chain proof, and the
GENESIS-to-tip chain verifies. Unique tenant per test + scoped teardown.
"""

from __future__ import annotations

import hashlib
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
        tenant_id = f"tnt_eu_exp_{sfx}"
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
                    "email": f"exp-{sfx}@example.com",
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
    text: str,
    subject_user_id: Optional[str] = None,
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
        subject_user_id=subject_user_id,
        record_metadata={
            "chat_id": chat_id,
            "message_id": msg_id,
            "role": role,
            "chat_name": "Requirements chat",
            "model": "claude-opus-4-7",
            "user_email": "user@example.com",
        },
    )


def _unzip(payload: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        return {n: zf.read(n).decode("utf-8") for n in zf.namelist()}


# ───────────────────────────────────────────────────────────────────────────
# service — bundle structure + the chain proof
# ───────────────────────────────────────────────────────────────────────────


def test_bundle_has_all_files_and_counts(make_tenant) -> None:
    from vargate_telemetry import content_export

    tenant, _ = make_tenant()
    _seed_content(tenant, "chat_A", "m1", "user", occurred_at=_T, text="hi")
    _seed_content(
        tenant, "chat_A", "m2", "assistant",
        occurred_at=_T.replace(hour=11), text="hello",
    )
    _seed_content(tenant, "chat_B", "m3", "user", occurred_at=_T, text="yo")

    filename, payload = content_export.build_export_bundle(
        tenant, generated_at=_GEN
    )
    assert filename == f"vargate-export-{tenant}-20260601T120000Z.zip"
    bundle = _unzip(payload)
    assert set(bundle) == {
        "manifest.json",
        "chats.json",
        "chain_proof.json",
        "README.txt",
    }
    manifest = json.loads(bundle["manifest.json"])
    assert manifest["counts"] == {
        "chats": 2,
        "messages": 3,
        "purged_messages": 0,
    }
    assert manifest["chain_verification"]["valid"] is True


def test_proof_content_hash_matches_exported_plaintext(make_tenant) -> None:
    """The export's load-bearing promise: SHA-256 of the exported text ==
    the content_hash carried in the chain proof for that record."""
    from vargate_telemetry import content_export

    tenant, _ = make_tenant()
    _seed_content(
        tenant, "chat_A", "m1", "user", occurred_at=_T, text="Hello, world!"
    )

    _, payload = content_export.build_export_bundle(tenant, generated_at=_GEN)
    bundle = _unzip(payload)
    chats = json.loads(bundle["chats.json"])["chats"]
    proof = json.loads(bundle["chain_proof.json"])

    msg = chats[0]["messages"][0]
    assert msg["content"] == "Hello, world!"
    rec = next(r for r in proof["records"] if r["external_id"] == "m1")
    assert (
        hashlib.sha256(msg["content"].encode("utf-8")).hexdigest()
        == rec["content_hash"]
    )
    assert proof["verification"]["valid"] is True
    # Chain linkage fields are present + hex.
    assert len(rec["chain_self_hash"]) == 64
    assert len(rec["chain_prev_hash"]) == 64
    assert isinstance(rec["chain_seq"], int)


def test_scope_filters_by_subject_and_date(make_tenant) -> None:
    from vargate_telemetry import content_export

    tenant, _ = make_tenant()
    u1 = str(uuid.uuid4())
    u2 = str(uuid.uuid4())
    _seed_content(
        tenant, "chat_1", "m1", "user",
        occurred_at=_T, text="u1 early", subject_user_id=u1,
    )
    _seed_content(
        tenant, "chat_2", "m2", "user",
        occurred_at=_T.replace(day=25), text="u1 late", subject_user_id=u1,
    )
    _seed_content(
        tenant, "chat_3", "m3", "user",
        occurred_at=_T, text="u2", subject_user_id=u2,
    )

    # subject filter → only u1's two messages.
    _, payload = content_export.build_export_bundle(
        tenant, generated_at=_GEN, subject_user_id=u1
    )
    proof = json.loads(_unzip(payload)["chain_proof.json"])
    assert {r["external_id"] for r in proof["records"]} == {"m1", "m2"}

    # date filter → only the early one (end exclusive).
    _, payload2 = content_export.build_export_bundle(
        tenant,
        generated_at=_GEN,
        subject_user_id=u1,
        end=datetime(2026, 5, 21, tzinfo=timezone.utc),
    )
    proof2 = json.loads(_unzip(payload2)["chain_proof.json"])
    assert {r["external_id"] for r in proof2["records"]} == {"m1"}


def test_purged_message_in_proof_without_content(make_tenant) -> None:
    """A purged message stays in the proof (existed) with content absent."""
    from vargate_telemetry import content_deletion, content_export

    tenant, _ = make_tenant()
    _seed_content(tenant, "chat_A", "m1", "user", occurred_at=_T, text="secret")
    content_deletion.delete_chat(
        tenant, "chat_A", reason="DSR", requested_by="admin"
    )

    _, payload = content_export.build_export_bundle(tenant, generated_at=_GEN)
    bundle = _unzip(payload)
    manifest = json.loads(bundle["manifest.json"])
    assert manifest["counts"]["purged_messages"] == 1

    msg = json.loads(bundle["chats.json"])["chats"][0]["messages"][0]
    assert msg["purged"] is True and msg["content"] is None
    # The chain proof still carries the record + its content_hash.
    rec = json.loads(bundle["chain_proof.json"])["records"][0]
    assert rec["purged"] is True and len(rec["content_hash"]) == 64
    # Chain still verifies after the deletion.
    assert manifest["chain_verification"]["valid"] is True


# ───────────────────────────────────────────────────────────────────────────
# endpoint
# ───────────────────────────────────────────────────────────────────────────


def test_export_endpoint_admin_returns_zip(client, make_tenant) -> None:
    tenant, admin = make_tenant()
    _seed_content(tenant, "chat_A", "m1", "user", occurred_at=_T, text="hi")
    resp = client.get("/content/export", headers=_bearer(admin, tenant))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]
    bundle = _unzip(resp.content)
    assert "chain_proof.json" in bundle
    assert json.loads(bundle["manifest.json"])["counts"]["messages"] == 1


def test_export_endpoint_forbidden_for_member(client, make_tenant) -> None:
    tenant, member = make_tenant(role="member")
    resp = client.get("/content/export", headers=_bearer(member, tenant))
    assert resp.status_code == 403, resp.text
