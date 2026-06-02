# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""The content surface is compliance-tier gated (require_content_capture).

A tenant with NO sealed Compliance Access Key — even an admin — must get
403 `compliance_tier_required` on every content endpoint (view / export /
delete / reveal), not an empty result. Closes the entitlement gap where a
non-compliance admin could reach the API directly.
"""

from __future__ import annotations

import uuid
from typing import Iterator

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


@pytest.fixture
def noncompliance_admin() -> Iterator[tuple[str, uuid.UUID]]:
    """An ADMIN of a tenant with a DEK but NO sealed Compliance Access Key
    → not on the compliance tier. (Admin so the only failing gate is
    content_capture, giving a deterministic 403 code.)"""
    from vargate_telemetry.crypto.seal import provision_tenant_dek
    from vargate_telemetry.db import engine

    tid = f"tnt_us_noncomp_{uuid.uuid4().hex[:12]}"
    uid = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "billing_status) VALUES (:t, 'us', true, 'paying')"
            ),
            {"t": tid},
        )
        conn.execute(
            sql_text(
                "INSERT INTO users (id, email, sso_provider, sso_subject_id, "
                "name, tenant_id, role) VALUES "
                "(:id, :email, 'google', :sub, 'Tester', :t, 'admin')"
            ),
            {
                "id": str(uid),
                "email": f"nc-{uid.hex[:8]}@example.com",
                "sub": f"sub-{uid}",
                "t": tid,
            },
        )
    provision_tenant_dek(tid)  # DEK present, but NO compliance key sealed
    yield tid, uid
    with engine.begin() as conn:
        for tbl in ("tenant_deks", "telemetry_records", "users", "tenants"):
            conn.execute(
                sql_text(f"DELETE FROM {tbl} WHERE tenant_id = :t"), {"t": tid}
            )


def _bearer(uid: uuid.UUID, tid: str) -> dict:
    from vargate_telemetry.auth.jwt import issue_session_jwt

    return {
        "Authorization": "Bearer "
        + issue_session_jwt(
            user_id=str(uid),
            email="tester@example.com",
            sso_provider="google",
            tenant_id=tid,
        )
    }


def _assert_gated(resp) -> None:
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "compliance_tier_required"


def test_list_requires_compliance_tier(client, noncompliance_admin) -> None:
    tid, uid = noncompliance_admin
    _assert_gated(client.get("/content/chats", headers=_bearer(uid, tid)))


def test_detail_requires_compliance_tier(client, noncompliance_admin) -> None:
    tid, uid = noncompliance_admin
    _assert_gated(
        client.get("/content/chats/x", headers=_bearer(uid, tid))
    )


def test_reveal_requires_compliance_tier(client, noncompliance_admin) -> None:
    tid, uid = noncompliance_admin
    _assert_gated(
        client.post("/content/chats/x/reveal", headers=_bearer(uid, tid))
    )


def test_delete_chat_requires_compliance_tier(client, noncompliance_admin) -> None:
    tid, uid = noncompliance_admin
    _assert_gated(
        client.request(
            "DELETE",
            "/content/chats/x",
            json={"reason": "x"},
            headers=_bearer(uid, tid),
        )
    )


def test_delete_user_requires_compliance_tier(client, noncompliance_admin) -> None:
    tid, uid = noncompliance_admin
    _assert_gated(
        client.request(
            "DELETE",
            f"/content/users/{uuid.uuid4()}",
            json={"reason": "x"},
            headers=_bearer(uid, tid),
        )
    )


def test_shred_requires_compliance_tier(client, noncompliance_admin) -> None:
    tid, uid = noncompliance_admin
    _assert_gated(
        client.post(
            "/content/tenant/shred",
            json={"reason": "x", "confirm_tenant_id": tid},
            headers=_bearer(uid, tid),
        )
    )


def test_export_requires_compliance_tier(client, noncompliance_admin) -> None:
    tid, uid = noncompliance_admin
    _assert_gated(
        client.get("/content/export", headers=_bearer(uid, tid))
    )
