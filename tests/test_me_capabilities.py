# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""TM2 Phase D2 / TM8 Phase B — GET /me/capabilities tests.

State-of-tenant capability snapshot. As of TM8 the response is a
NESTED per-vendor map ``{anthropic:{…}, openai:{…}}`` that ALSO
dual-emits the legacy flat Anthropic keys at the top level for one
release (so the current SPA keeps working mid-deploy).

Anthropic bools answer "does the tenant have at least one
telemetry_records row with the matching source_api in the last 90
days" (``content_capture`` is the exception — a sealed Compliance
Access Key). OpenAI bools (TM8):

  - ``admin``             — recent ``openai_admin_usage`` row OR a
                            sealed ``openai_admin_key``.
  - ``costs``            — recent ``openai_admin_costs`` row.
  - ``audit_logs``       — recent ``openai_audit_logs`` row
                            (accessible ≠ populated; stays False until
                            a row actually lands).
  - ``project_users``    — the ``openai_users`` side table has rows.
  - ``per_user_breakdown`` — a recent ``openai_admin_usage`` row has a
                            non-null ``subject_user_id``.

Cases:

  - No session → 401.
  - Pre-tenant user (no tenant_id) → nested all-False + flat all-False.
  - Tenant with no data → nested all-False + flat all-False.
  - Tenant with only mcp rows → mcp_connector True, the rest False;
    flat keys mirror anthropic.*.
  - Tenant with admin + activity_feed + code_analytics + mcp rows
    → four anthropic True, content_capture False.
  - content_capture is False unless a Compliance Access Key is sealed.
  - OpenAI: sealed key lights ``admin`` with zero rows; usage rows
    light ``admin`` + (when subject_user_id present) per_user_breakdown;
    a null-subject usage row lights ``admin`` but NOT per_user_breakdown;
    costs / audit_logs rows light their flags; an ``openai_users`` row
    lights project_users; old rows don't count.
  - Cross-check: the flat top-level keys ALWAYS equal anthropic.* and
    OpenAI never leaks into the flat keys.
"""

from __future__ import annotations

from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

# Anthropic flat / nested keys (the dual-emit contract) + OpenAI nested
# keys, named once so the assertions can't drift from the response shape.
_ANTHROPIC_KEYS = (
    "admin_api",
    "activity_feed",
    "content_capture",
    "code_analytics",
    "mcp_connector",
)
_OPENAI_KEYS = (
    "admin",
    "costs",
    "audit_logs",
    "project_users",
    "per_user_breakdown",
)


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


@pytest.fixture
def clean_records() -> Iterator[None]:
    """Truncate telemetry + the OpenAI side tables + per-tenant secret
    state so each case starts clean.

    The OpenAI ``admin`` flag can light on a sealed ``openai_admin_key``
    in ``encrypted_secrets``, and ``project_users`` reads the
    ``openai_users`` side table — both must be wiped between tests, not
    just ``telemetry_records``. ``tenant_deks`` / ``tenants`` get cleaned
    too because sealing a key needs a provisioned DEK (which needs a
    tenant row). CASCADE on telemetry covers chain side-effects.
    """
    from vargate_telemetry.db import engine

    def _wipe(conn) -> None:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE"
            )
        )
        # encrypted_secrets / tenant_deks FK to tenants; openai_users
        # FKs to tenants ON DELETE CASCADE. Truncate them all together so
        # ordering / FK direction doesn't matter.
        conn.execute(
            sql_text(
                "TRUNCATE TABLE encrypted_secrets, tenant_deks, "
                "openai_users, tenants RESTART IDENTITY CASCADE"
            )
        )

    with engine.begin() as conn:
        _wipe(conn)
    yield
    with engine.begin() as conn:
        _wipe(conn)


def _bearer(*, tenant_id: str | None = "tnt_us_capabilities_test") -> dict[str, str]:
    from vargate_telemetry.auth.jwt import issue_session_jwt

    token = issue_session_jwt(
        user_id="user-capabilities-test",
        email="capabilities@example.com",
        sso_provider="google",
        tenant_id=tenant_id,
    )
    return {"Authorization": f"Bearer {token}"}


def _insert_record(
    *,
    tenant_id: str,
    source_api: str,
    chain_seq: int,
    days_old: int = 0,
    subject_user_id: str | None = "u",
) -> None:
    """Raw SQL insert — bypasses the chain primitive so we can plant
    records for any source_api without going through pull tasks.

    ``subject_user_id`` is a real ``telemetry_records`` column; pass
    ``None`` to model an OpenAI usage row whose ``group_by=user_id`` did
    not populate (so ``per_user_breakdown`` stays False)."""
    from vargate_telemetry.db import engine

    zero32 = b"\x00" * 32

    with engine.begin() as conn:
        conn.execute(sql_text("SET LOCAL ROLE vargate_app"))
        conn.execute(
            sql_text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": tenant_id},
        )
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    subject_user_id, occurred_at, ingested_at,
                    content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, 'cap_test', :s, :ext,
                    :subj,
                    now() - (:days * INTERVAL '1 day'),
                    now() - (:days * INTERVAL '1 day'),
                    :h, '{}'::jsonb,
                    :seq, :h, :h
                )
                """
            ),
            {
                "t": tenant_id,
                "s": source_api,
                "ext": f"cap-test-{source_api}-{chain_seq}",
                "days": days_old,
                "h": zero32,
                "seq": chain_seq,
                "subj": subject_user_id,
            },
        )


def _seal_openai_key(tenant_id: str) -> None:
    """Provision a tenant + DEK and seal a fake ``openai_admin_key`` so
    the OpenAI ``admin`` flag lights on key-presence (before any pull)."""
    from vargate_telemetry.crypto.seal import provision_tenant_dek, seal_secret
    from vargate_telemetry.db import engine
    from vargate_telemetry.openai.factory import OPENAI_ADMIN_KEY_SECRET

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "billing_status) VALUES (:t, 'us', true, 'trial') "
                "ON CONFLICT (tenant_id) DO NOTHING"
            ),
            {"t": tenant_id},
        )
    provision_tenant_dek(tenant_id)
    seal_secret(tenant_id, OPENAI_ADMIN_KEY_SECRET, b"sk-admin-test-openai")


def _insert_openai_user(tenant_id: str, *, openai_user_id: str = "user-x") -> None:
    """Plant one ``openai_users`` row (the projects sync result) so the
    ``project_users`` flag lights."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "billing_status) VALUES (:t, 'us', true, 'trial') "
                "ON CONFLICT (tenant_id) DO NOTHING"
            ),
            {"t": tenant_id},
        )
        conn.execute(sql_text("SET LOCAL ROLE vargate_app"))
        conn.execute(
            sql_text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": tenant_id},
        )
        conn.execute(
            sql_text(
                """
                INSERT INTO openai_users
                    (tenant_id, openai_user_id, email, name, role)
                VALUES (:t, :uid, :email, 'OpenAI User', 'member')
                ON CONFLICT (tenant_id, openai_user_id) DO NOTHING
                """
            ),
            {
                "t": tenant_id,
                "uid": openai_user_id,
                "email": f"{openai_user_id}@example.com",
            },
        )


def _assert_shape(body: dict) -> None:
    """Every response carries the nested per-vendor map AND the flat
    legacy Anthropic keys; the flat keys MUST mirror anthropic.* exactly,
    and OpenAI must never appear at the top level."""
    assert set(body["anthropic"].keys()) == set(_ANTHROPIC_KEYS)
    assert set(body["openai"].keys()) == set(_OPENAI_KEYS)
    for k in _ANTHROPIC_KEYS:
        assert body[k] == body["anthropic"][k], (
            f"flat {k}={body[k]} must mirror anthropic.{k}="
            f"{body['anthropic'][k]}"
        )
    # OpenAI flags never leak into the flat top-level surface.
    for k in _OPENAI_KEYS:
        if k not in _ANTHROPIC_KEYS:
            assert k not in body or k in _ANTHROPIC_KEYS


# ───────────────────────────────────────────────────────────────────────────
# Auth + empty cases
# ───────────────────────────────────────────────────────────────────────────


def test_no_session_returns_401(client: TestClient) -> None:
    response = client.get("/me/capabilities")
    assert response.status_code == 401


def test_pre_tenant_user_gets_all_false(
    client: TestClient,
    clean_records: None,
) -> None:
    """A mid-onboarding caller (tenant_id=None) gets every bool False —
    in BOTH the nested vendors and the flat legacy keys."""
    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=None)
    )
    assert response.status_code == 200
    body = response.json()
    _assert_shape(body)
    assert body["anthropic"] == {
        "admin_api": False,
        "activity_feed": False,
        "content_capture": False,
        "code_analytics": False,
        "mcp_connector": False,
    }
    assert body["openai"] == {
        "admin": False,
        "costs": False,
        "audit_logs": False,
        "project_users": False,
        "per_user_breakdown": False,
    }
    # Flat legacy keys retained + all False.
    assert body["admin_api"] is False
    assert body["activity_feed"] is False
    assert body["content_capture"] is False
    assert body["code_analytics"] is False
    assert body["mcp_connector"] is False


def test_tenant_with_no_data_gets_all_false(
    client: TestClient,
    clean_records: None,
) -> None:
    """A tenant exists but has no rows yet — every bool False, both
    nested and flat."""
    response = client.get(
        "/me/capabilities",
        headers=_bearer(tenant_id="tnt_us_empty_cap"),
    )
    body = response.json()
    _assert_shape(body)
    assert all(v is False for v in body["anthropic"].values())
    assert all(v is False for v in body["openai"].values())


# ───────────────────────────────────────────────────────────────────────────
# Populated tenants — Anthropic
# ───────────────────────────────────────────────────────────────────────────


def test_only_mcp_rows_lights_mcp_connector(
    client: TestClient,
    clean_records: None,
) -> None:
    """A tenant whose only ingest path is MCP gets mcp_connector=True
    only; the flat mcp_connector mirrors anthropic.mcp_connector; OpenAI
    stays all-False."""
    tenant = "tnt_us_only_mcp"
    _insert_record(tenant_id=tenant, source_api="mcp", chain_seq=1)
    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=tenant)
    )
    body = response.json()
    _assert_shape(body)
    assert body["anthropic"]["mcp_connector"] is True
    assert body["anthropic"]["admin_api"] is False
    assert body["anthropic"]["activity_feed"] is False
    assert body["anthropic"]["code_analytics"] is False
    # Flat key retained + mirrors nested.
    assert body["mcp_connector"] is True
    # OpenAI untouched.
    assert all(v is False for v in body["openai"].values())


def test_all_four_sources_light_their_capabilities(
    client: TestClient,
    clean_records: None,
) -> None:
    """Admin + activity_feed + code_analytics + mcp rows → four
    anthropic True, content_capture stays False (T5.3 invariant)."""
    tenant = "tnt_us_all_four"
    _insert_record(tenant_id=tenant, source_api="admin", chain_seq=1)
    _insert_record(
        tenant_id=tenant, source_api="compliance_activities", chain_seq=2
    )
    _insert_record(
        tenant_id=tenant, source_api="code_analytics", chain_seq=3
    )
    _insert_record(tenant_id=tenant, source_api="mcp", chain_seq=4)

    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=tenant)
    )
    body = response.json()
    _assert_shape(body)
    a = body["anthropic"]
    assert a["admin_api"] is True
    assert a["activity_feed"] is True
    assert a["code_analytics"] is True
    assert a["mcp_connector"] is True
    # T5.3 invariant: even with all four real sources active,
    # content_capture is always False (no Compliance Access Key sealed).
    assert a["content_capture"] is False
    # OpenAI source_api strings are disjoint from Anthropic's → no spill.
    assert all(v is False for v in body["openai"].values())


def test_content_capture_requires_sealed_compliance_key(
    client: TestClient,
    clean_records: None,
) -> None:
    """An ``openai_admin_usage`` row (or any non-content row) does NOT
    flip Anthropic ``content_capture`` — only a sealed Compliance Access
    Key does. Plant a usage row and confirm content_capture stays False
    while OpenAI ``admin`` flips True."""
    tenant = "tnt_us_content_attempt"
    _insert_record(
        tenant_id=tenant, source_api="openai_admin_usage", chain_seq=1
    )
    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=tenant)
    )
    body = response.json()
    _assert_shape(body)
    assert body["anthropic"]["content_capture"] is False
    assert body["content_capture"] is False
    assert body["openai"]["admin"] is True


def test_old_rows_dont_count(
    client: TestClient,
    clean_records: None,
) -> None:
    """A 91-day-old row is outside the recent-activity window — for both
    Anthropic admin and OpenAI usage."""
    tenant = "tnt_us_old_only"
    _insert_record(
        tenant_id=tenant, source_api="admin", chain_seq=1, days_old=91
    )
    _insert_record(
        tenant_id=tenant,
        source_api="openai_admin_usage",
        chain_seq=2,
        days_old=91,
    )
    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=tenant)
    )
    body = response.json()
    _assert_shape(body)
    assert body["anthropic"]["admin_api"] is False
    # 91-day-old usage doesn't light admin or per_user_breakdown.
    assert body["openai"]["admin"] is False
    assert body["openai"]["per_user_breakdown"] is False


# ───────────────────────────────────────────────────────────────────────────
# Populated tenants — OpenAI (TM8)
# ───────────────────────────────────────────────────────────────────────────


def test_openai_admin_lights_on_sealed_key_with_no_rows(
    client: TestClient,
    clean_records: None,
) -> None:
    """A sealed ``openai_admin_key`` lights ``openai.admin`` even before
    the first pull lands a row (so the onboarding tile flips on seal).
    Nothing else lights, and the flat Anthropic keys stay False."""
    tenant = "tnt_us_oai_key_only"
    _seal_openai_key(tenant)
    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=tenant)
    )
    body = response.json()
    _assert_shape(body)
    assert body["openai"]["admin"] is True
    assert body["openai"]["costs"] is False
    assert body["openai"]["audit_logs"] is False
    assert body["openai"]["project_users"] is False
    assert body["openai"]["per_user_breakdown"] is False
    # A sealed OpenAI key must NOT light any Anthropic flag.
    assert all(v is False for v in body["anthropic"].values())


def test_openai_usage_row_lights_admin_and_per_user_breakdown(
    client: TestClient,
    clean_records: None,
) -> None:
    """A recent ``openai_admin_usage`` row with a non-null
    subject_user_id lights ``admin`` AND ``per_user_breakdown``."""
    tenant = "tnt_us_oai_usage"
    _insert_record(
        tenant_id=tenant,
        source_api="openai_admin_usage",
        chain_seq=1,
        subject_user_id="user-abc",
    )
    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=tenant)
    )
    body = response.json()
    _assert_shape(body)
    assert body["openai"]["admin"] is True
    assert body["openai"]["per_user_breakdown"] is True
    # No costs / audit / users rows planted.
    assert body["openai"]["costs"] is False
    assert body["openai"]["audit_logs"] is False
    assert body["openai"]["project_users"] is False


def test_openai_usage_with_null_subject_does_not_light_per_user(
    client: TestClient,
    clean_records: None,
) -> None:
    """A usage row whose ``group_by=user_id`` didn't populate (null
    subject_user_id) lights ``admin`` but NOT ``per_user_breakdown`` —
    the honest empty-state signal for coarse-tier orgs."""
    tenant = "tnt_us_oai_usage_null"
    _insert_record(
        tenant_id=tenant,
        source_api="openai_admin_usage",
        chain_seq=1,
        subject_user_id=None,
    )
    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=tenant)
    )
    body = response.json()
    _assert_shape(body)
    assert body["openai"]["admin"] is True
    assert body["openai"]["per_user_breakdown"] is False


def test_openai_costs_row_lights_costs(
    client: TestClient,
    clean_records: None,
) -> None:
    """A recent ``openai_admin_costs`` row lights ``costs`` (and NOT
    ``admin``, since costs alone doesn't imply usage rows / sealed key —
    though in practice both arrive together)."""
    tenant = "tnt_us_oai_costs"
    _insert_record(
        tenant_id=tenant, source_api="openai_admin_costs", chain_seq=1
    )
    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=tenant)
    )
    body = response.json()
    _assert_shape(body)
    assert body["openai"]["costs"] is True
    assert body["openai"]["admin"] is False
    assert body["openai"]["per_user_breakdown"] is False


def test_openai_audit_row_lights_audit_logs(
    client: TestClient,
    clean_records: None,
) -> None:
    """A recent ``openai_audit_logs`` row lights ``audit_logs`` — the
    accessible-AND-populated case (recall the endpoint 200s-but-empty on
    non-Enterprise orgs, where this stays False)."""
    tenant = "tnt_us_oai_audit"
    _insert_record(
        tenant_id=tenant, source_api="openai_audit_logs", chain_seq=1
    )
    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=tenant)
    )
    body = response.json()
    _assert_shape(body)
    assert body["openai"]["audit_logs"] is True


def test_openai_users_side_table_lights_project_users(
    client: TestClient,
    clean_records: None,
) -> None:
    """An ``openai_users`` row (the projects sync result) lights
    ``project_users`` independent of any telemetry rows."""
    tenant = "tnt_us_oai_proj_users"
    _insert_openai_user(tenant)
    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=tenant)
    )
    body = response.json()
    _assert_shape(body)
    assert body["openai"]["project_users"] is True
    # No usage / costs / audit rows + no sealed key planted.
    assert body["openai"]["admin"] is False
    assert body["openai"]["costs"] is False
    assert body["openai"]["audit_logs"] is False


def test_openai_does_not_leak_into_flat_keys(
    client: TestClient,
    clean_records: None,
) -> None:
    """Belt-and-braces: a fully-lit OpenAI tenant leaves every flat
    legacy Anthropic key False (the flat keys mirror anthropic.* ONLY)."""
    tenant = "tnt_us_oai_full"
    _seal_openai_key(tenant)
    _insert_record(
        tenant_id=tenant,
        source_api="openai_admin_usage",
        chain_seq=1,
        subject_user_id="user-z",
    )
    _insert_record(
        tenant_id=tenant, source_api="openai_admin_costs", chain_seq=2
    )
    _insert_record(
        tenant_id=tenant, source_api="openai_audit_logs", chain_seq=3
    )
    _insert_openai_user(tenant)

    response = client.get(
        "/me/capabilities", headers=_bearer(tenant_id=tenant)
    )
    body = response.json()
    _assert_shape(body)
    # All OpenAI flags lit…
    assert all(body["openai"][k] is True for k in _OPENAI_KEYS)
    # …but every flat legacy key (== anthropic.*) is False.
    assert all(body[k] is False for k in _ANTHROPIC_KEYS)
    assert all(body["anthropic"][k] is False for k in _ANTHROPIC_KEYS)
