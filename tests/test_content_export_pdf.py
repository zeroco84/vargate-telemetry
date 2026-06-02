# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the PDF eDiscovery export (PDF export spec). Seeds REAL
content, renders the PDF, and parses it with pypdf to assert the cover /
transcript / Bates numbering / proof appendix / redaction. Plus the
combined-zip format + the endpoint format dispatch."""

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
from pypdf import PdfReader
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
        tenant_id = f"tnt_eu_pdf_{sfx}"
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
                    "email": f"pdf-{sfx}@example.com",
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


def _seed(tenant_id: str, chat_id: str, msg_id: str, *, text: str) -> None:
    from vargate_telemetry.chain import append_telemetry_record
    from vargate_telemetry.storage.content import store_content

    content_ref, content_hash, size = store_content(tenant_id, text.encode())
    append_telemetry_record(
        tenant_id,
        record_type="chat_message",
        source_api="compliance_content",
        external_id=msg_id,
        occurred_at=_T,
        content_hash=content_hash,
        content_ref=content_ref,
        content_size_bytes=size,
        record_metadata={
            "chat_id": chat_id,
            "message_id": msg_id,
            "role": "user",
            "chat_name": "Demo chat",
            "model": "claude-opus-4-7",
            "user_email": "user@example.com",
        },
    )


def _pdf_text(payload: bytes) -> str:
    reader = PdfReader(io.BytesIO(payload))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# ───────────────────────────────────────────────────────────────────────────
# render_pdf
# ───────────────────────────────────────────────────────────────────────────


def test_pdf_has_cover_transcript_and_bates(make_tenant) -> None:
    from vargate_telemetry import content_export

    tenant, _ = make_tenant()
    _seed(tenant, "chat_A", "m1", text="Draft the launch plan please")
    _seed(tenant, "chat_A", "m2", text="Sure, here is the outline")

    fname, payload = content_export.render_pdf(
        content_export.gather_export_model(tenant, generated_at=_GEN)
    )
    assert fname.endswith(".pdf")
    assert payload.startswith(b"%PDF-")
    text = _pdf_text(payload)
    assert "CONFIDENTIAL" in text
    assert tenant in text
    assert "Draft the launch plan please" in text  # transcript content
    assert "VARGATE-000001" in text  # Bates numbering
    assert "Appendix A" in text  # chain-proof appendix
    assert "valid=True" in text  # chain verdict on the cover


def test_pdf_masks_pii_by_default_and_reveals_with_flag(make_tenant) -> None:
    from vargate_telemetry import content_export

    tenant, _ = make_tenant()
    _seed(tenant, "chat_A", "m1", text="Email alice@secret.example.com now")

    # redacted (default) → the email must NOT appear in the rendered text
    _, masked = content_export.render_pdf(
        content_export.gather_export_model(tenant, generated_at=_GEN, redact=True)
    )
    assert "alice@secret.example.com" not in _pdf_text(masked)

    # reveal (redact=False) → full content present
    _, full = content_export.render_pdf(
        content_export.gather_export_model(tenant, generated_at=_GEN, redact=False)
    )
    assert "alice@secret.example.com" in _pdf_text(full)


def test_pdf_renders_purged_tombstone(make_tenant) -> None:
    from vargate_telemetry import content_deletion, content_export

    tenant, _ = make_tenant()
    _seed(tenant, "chat_A", "m1", text="secret content here")
    content_deletion.delete_chat(
        tenant, "chat_A", reason="demo", requested_by="admin"
    )
    _, payload = content_export.render_pdf(
        content_export.gather_export_model(tenant, generated_at=_GEN)
    )
    text = _pdf_text(payload)
    assert "secret content here" not in text
    assert "content deleted" in text.lower()


# ───────────────────────────────────────────────────────────────────────────
# combined zip (format=both)
# ───────────────────────────────────────────────────────────────────────────


def test_combined_zip_has_json_bundle_plus_pdf(make_tenant) -> None:
    from vargate_telemetry import content_export

    tenant, _ = make_tenant()
    _seed(tenant, "chat_A", "m1", text="hello")

    fname, payload, media = content_export.build_export(
        tenant, fmt="both", generated_at=_GEN
    )
    assert media == "application/zip"
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = set(zf.namelist())
        assert {
            "manifest.json",
            "chats.json",
            "chain_proof.json",
            "README.txt",
            "export.pdf",
        } <= names
        manifest = json.loads(zf.read("manifest.json"))
        assert "export.pdf" in manifest["files"]
        assert zf.read("export.pdf").startswith(b"%PDF-")


def test_build_export_rejects_bad_format(make_tenant) -> None:
    from vargate_telemetry import content_export

    tenant, _ = make_tenant()
    with pytest.raises(ValueError):
        content_export.build_export(tenant, fmt="docx", generated_at=_GEN)


# ───────────────────────────────────────────────────────────────────────────
# endpoint format dispatch
# ───────────────────────────────────────────────────────────────────────────


def test_endpoint_pdf(client, make_tenant) -> None:
    tenant, admin = make_tenant()
    _seed(tenant, "chat_A", "m1", text="hello pdf")
    resp = client.get(
        "/content/export?format=pdf", headers=_bearer(admin, tenant)
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"%PDF-")


def test_endpoint_both(client, make_tenant) -> None:
    tenant, admin = make_tenant()
    _seed(tenant, "chat_A", "m1", text="hello both")
    resp = client.get(
        "/content/export?format=both", headers=_bearer(admin, tenant)
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert "export.pdf" in zf.namelist()


def test_endpoint_bad_format_400(client, make_tenant) -> None:
    tenant, admin = make_tenant()
    resp = client.get(
        "/content/export?format=docx", headers=_bearer(admin, tenant)
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "invalid_format"


def test_endpoint_pdf_forbidden_for_member(client, make_tenant) -> None:
    tenant, member = make_tenant(role="member")
    resp = client.get(
        "/content/export?format=pdf", headers=_bearer(member, tenant)
    )
    assert resp.status_code == 403, resp.text
