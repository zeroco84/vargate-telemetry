# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the OpenAI side tables (migration 0025, TM8 Phase B).

These are the OpenAI analogue of the Anthropic ``workspaces`` /
``api_keys`` side tables — raw-SQL-only (no ORM model), upserted by
the projects pull task, joined into the usage SQL to resolve opaque
vendor IDs to names + emails.

Asserts the migration produced tables that:
  - round-trip an insert + read-back under an RLS-scoped session
  - upsert on the composite PK (``ON CONFLICT DO UPDATE``), the
    ``_sync_*`` idempotency posture
  - are RLS-isolated — one tenant's rows are invisible to another
    under ``session_scope`` (FORCE ROW LEVEL SECURITY)
  - cascade-delete with their tenant (``ON DELETE CASCADE``)
  - default ``synced_at`` to ``now()`` server-side
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

import pytest
from sqlalchemy import text as sql_text

from vargate_telemetry.db import engine, session_scope

_TENANT_A = "tnt_us_openai_sidetbl_a"
_TENANT_B = "tnt_us_openai_sidetbl_b"

_OPENAI_TABLES = ("openai_projects", "openai_api_keys", "openai_users")


@pytest.fixture
def clean_state() -> Iterator[None]:
    """Truncate the three side tables + the test tenants around each test.

    ``engine.begin()`` runs as the bootstrap superuser (bypasses RLS),
    so the TRUNCATE clears every tenant's rows. CASCADE so the tenant
    delete (which the cascade test exercises) doesn't trip FK refs.
    """

    def _truncate() -> None:
        with engine.begin() as conn:
            for table in _OPENAI_TABLES:
                conn.execute(
                    sql_text(
                        f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE"
                    )
                )
            conn.execute(
                sql_text(
                    "DELETE FROM tenants WHERE tenant_id IN (:a, :b)"
                ),
                {"a": _TENANT_A, "b": _TENANT_B},
            )

    _truncate()
    yield
    _truncate()


def _provision_tenant(tenant_id: str) -> None:
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


def _insert_project(
    tenant_id: str,
    project_id: str,
    *,
    name: str,
    status: str | None = "active",
    created_at: datetime | None = None,
) -> None:
    """Mirror the ``_sync_*`` upsert posture the pull task will use."""
    with session_scope(tenant_id) as s:
        s.execute(
            sql_text(
                """
                INSERT INTO openai_projects
                    (tenant_id, project_id, name, status,
                     created_at_openai)
                VALUES (:tenant_id, :project_id, :name, :status, :created)
                ON CONFLICT (tenant_id, project_id)
                DO UPDATE SET
                    name = EXCLUDED.name,
                    status = EXCLUDED.status,
                    created_at_openai = EXCLUDED.created_at_openai,
                    synced_at = now()
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "name": name,
                "status": status,
                "created": created_at,
            },
        )


# ───────────────────────────────────────────────────────────────────────────
# openai_projects
# ───────────────────────────────────────────────────────────────────────────


def test_project_roundtrip_and_synced_at_default(
    clean_state: None,
) -> None:
    _provision_tenant(_TENANT_A)
    created = datetime(2024, 7, 22, 18, 10, 6, tzinfo=timezone.utc)
    _insert_project(
        _TENANT_A, "proj_alpha", name="Alpha", created_at=created
    )

    with session_scope(_TENANT_A) as s:
        row = s.execute(
            sql_text(
                """
                SELECT name, status, created_at_openai, synced_at
                FROM openai_projects
                WHERE tenant_id = :t AND project_id = :p
                """
            ),
            {"t": _TENANT_A, "p": "proj_alpha"},
        ).one()

    assert row.name == "Alpha"
    assert row.status == "active"
    assert row.created_at_openai == created
    # server_default=now() populated the row even though we never set it.
    assert row.synced_at is not None


def test_project_upsert_on_conflict(clean_state: None) -> None:
    """Re-syncing the same project id updates name/status, not duplicates."""
    _provision_tenant(_TENANT_A)
    _insert_project(
        _TENANT_A, "proj_alpha", name="Alpha", status="active"
    )
    _insert_project(
        _TENANT_A, "proj_alpha", name="Alpha Renamed", status="archived"
    )

    with session_scope(_TENANT_A) as s:
        count = s.execute(
            sql_text(
                "SELECT count(*) FROM openai_projects "
                "WHERE tenant_id = :t"
            ),
            {"t": _TENANT_A},
        ).scalar_one()
        row = s.execute(
            sql_text(
                "SELECT name, status FROM openai_projects "
                "WHERE tenant_id = :t AND project_id = :p"
            ),
            {"t": _TENANT_A, "p": "proj_alpha"},
        ).one()

    assert count == 1
    assert row.name == "Alpha Renamed"
    assert row.status == "archived"


def test_project_status_nullable(clean_state: None) -> None:
    _provision_tenant(_TENANT_A)
    _insert_project(
        _TENANT_A, "proj_nostatus", name="No Status", status=None
    )
    with session_scope(_TENANT_A) as s:
        status = s.execute(
            sql_text(
                "SELECT status FROM openai_projects "
                "WHERE tenant_id = :t AND project_id = :p"
            ),
            {"t": _TENANT_A, "p": "proj_nostatus"},
        ).scalar_one()
    assert status is None


def test_projects_rls_isolated(clean_state: None) -> None:
    """Tenant B never sees tenant A's projects under session_scope."""
    _provision_tenant(_TENANT_A)
    _provision_tenant(_TENANT_B)
    _insert_project(_TENANT_A, "proj_alpha", name="Alpha")
    _insert_project(_TENANT_B, "proj_beta", name="Beta")

    with session_scope(_TENANT_B) as s:
        ids = set(
            s.execute(
                sql_text("SELECT project_id FROM openai_projects")
            ).scalars()
        )
    # FORCE RLS hides A's row; B sees only its own.
    assert ids == {"proj_beta"}


def test_project_cascades_on_tenant_delete(clean_state: None) -> None:
    """Deleting the tenant removes its projects (ON DELETE CASCADE)."""
    _provision_tenant(_TENANT_A)
    _insert_project(_TENANT_A, "proj_alpha", name="Alpha")

    with engine.begin() as conn:
        conn.execute(
            sql_text("DELETE FROM tenants WHERE tenant_id = :t"),
            {"t": _TENANT_A},
        )
        # Read back as superuser (RLS bypassed) — the row is gone.
        remaining = conn.execute(
            sql_text(
                "SELECT count(*) FROM openai_projects "
                "WHERE tenant_id = :t"
            ),
            {"t": _TENANT_A},
        ).scalar_one()
    assert remaining == 0


# ───────────────────────────────────────────────────────────────────────────
# openai_api_keys
# ───────────────────────────────────────────────────────────────────────────


def test_api_key_roundtrip_and_nullable_optionals(
    clean_state: None,
) -> None:
    _provision_tenant(_TENANT_A)
    created = datetime(2024, 7, 22, 18, 10, 6, tzinfo=timezone.utc)
    last_used = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)
    with session_scope(_TENANT_A) as s:
        s.execute(
            sql_text(
                """
                INSERT INTO openai_api_keys
                    (tenant_id, api_key_id, project_id, name,
                     created_at_openai, last_used_at)
                VALUES (:t, :k, :p, :n, :created, :used)
                ON CONFLICT (tenant_id, api_key_id)
                DO UPDATE SET
                    project_id = EXCLUDED.project_id,
                    name = EXCLUDED.name,
                    created_at_openai = EXCLUDED.created_at_openai,
                    last_used_at = EXCLUDED.last_used_at,
                    synced_at = now()
                """
            ),
            {
                "t": _TENANT_A,
                "k": "key_alpha",
                "p": "proj_alpha",
                "n": "CI key",
                "created": created,
                "used": last_used,
            },
        )

    with session_scope(_TENANT_A) as s:
        row = s.execute(
            sql_text(
                """
                SELECT project_id, name, created_at_openai,
                       last_used_at, synced_at
                FROM openai_api_keys
                WHERE tenant_id = :t AND api_key_id = :k
                """
            ),
            {"t": _TENANT_A, "k": "key_alpha"},
        ).one()

    assert row.project_id == "proj_alpha"
    assert row.name == "CI key"
    assert row.created_at_openai == created
    assert row.last_used_at == last_used
    assert row.synced_at is not None


def test_api_key_minimal_row_allows_nulls(clean_state: None) -> None:
    """project_id / name / created / last_used are all nullable."""
    _provision_tenant(_TENANT_A)
    with session_scope(_TENANT_A) as s:
        s.execute(
            sql_text(
                """
                INSERT INTO openai_api_keys (tenant_id, api_key_id)
                VALUES (:t, :k)
                """
            ),
            {"t": _TENANT_A, "k": "key_bare"},
        )
        row = s.execute(
            sql_text(
                """
                SELECT project_id, name, created_at_openai, last_used_at
                FROM openai_api_keys
                WHERE tenant_id = :t AND api_key_id = :k
                """
            ),
            {"t": _TENANT_A, "k": "key_bare"},
        ).one()
    assert row.project_id is None
    assert row.name is None
    assert row.created_at_openai is None
    assert row.last_used_at is None


def test_api_keys_rls_isolated(clean_state: None) -> None:
    _provision_tenant(_TENANT_A)
    _provision_tenant(_TENANT_B)
    for tid, kid in ((_TENANT_A, "key_a"), (_TENANT_B, "key_b")):
        with session_scope(tid) as s:
            s.execute(
                sql_text(
                    "INSERT INTO openai_api_keys (tenant_id, api_key_id) "
                    "VALUES (:t, :k)"
                ),
                {"t": tid, "k": kid},
            )

    with session_scope(_TENANT_A) as s:
        ids = set(
            s.execute(
                sql_text("SELECT api_key_id FROM openai_api_keys")
            ).scalars()
        )
    assert ids == {"key_a"}


def test_api_key_cascades_on_tenant_delete(clean_state: None) -> None:
    _provision_tenant(_TENANT_A)
    with session_scope(_TENANT_A) as s:
        s.execute(
            sql_text(
                "INSERT INTO openai_api_keys (tenant_id, api_key_id) "
                "VALUES (:t, :k)"
            ),
            {"t": _TENANT_A, "k": "key_alpha"},
        )
    with engine.begin() as conn:
        conn.execute(
            sql_text("DELETE FROM tenants WHERE tenant_id = :t"),
            {"t": _TENANT_A},
        )
        remaining = conn.execute(
            sql_text(
                "SELECT count(*) FROM openai_api_keys "
                "WHERE tenant_id = :t"
            ),
            {"t": _TENANT_A},
        ).scalar_one()
    assert remaining == 0


# ───────────────────────────────────────────────────────────────────────────
# openai_users
# ───────────────────────────────────────────────────────────────────────────


def test_user_roundtrip_with_email(clean_state: None) -> None:
    """The email column carries PII used for cross-vendor alias match."""
    _provision_tenant(_TENANT_A)
    with session_scope(_TENANT_A) as s:
        s.execute(
            sql_text(
                """
                INSERT INTO openai_users
                    (tenant_id, openai_user_id, email, name, role)
                VALUES (:t, :u, :e, :n, :r)
                ON CONFLICT (tenant_id, openai_user_id)
                DO UPDATE SET
                    email = EXCLUDED.email,
                    name = EXCLUDED.name,
                    role = EXCLUDED.role,
                    synced_at = now()
                """
            ),
            {
                "t": _TENANT_A,
                "u": "user-alpha",
                "e": "alice@example.com",
                "n": "Alice",
                "r": "owner",
            },
        )
        row = s.execute(
            sql_text(
                """
                SELECT email, name, role, synced_at
                FROM openai_users
                WHERE tenant_id = :t AND openai_user_id = :u
                """
            ),
            {"t": _TENANT_A, "u": "user-alpha"},
        ).one()
    assert row.email == "alice@example.com"
    assert row.name == "Alice"
    assert row.role == "owner"
    assert row.synced_at is not None


def test_user_email_nullable(clean_state: None) -> None:
    """A user row with no email is allowed (coarser-tier orgs)."""
    _provision_tenant(_TENANT_A)
    with session_scope(_TENANT_A) as s:
        s.execute(
            sql_text(
                "INSERT INTO openai_users (tenant_id, openai_user_id) "
                "VALUES (:t, :u)"
            ),
            {"t": _TENANT_A, "u": "user-noemail"},
        )
        email = s.execute(
            sql_text(
                "SELECT email FROM openai_users "
                "WHERE tenant_id = :t AND openai_user_id = :u"
            ),
            {"t": _TENANT_A, "u": "user-noemail"},
        ).scalar_one()
    assert email is None


def test_users_rls_isolated(clean_state: None) -> None:
    _provision_tenant(_TENANT_A)
    _provision_tenant(_TENANT_B)
    for tid, uid, email in (
        (_TENANT_A, "user-a", "a@example.com"),
        (_TENANT_B, "user-b", "b@example.com"),
    ):
        with session_scope(tid) as s:
            s.execute(
                sql_text(
                    "INSERT INTO openai_users "
                    "(tenant_id, openai_user_id, email) "
                    "VALUES (:t, :u, :e)"
                ),
                {"t": tid, "u": uid, "e": email},
            )

    with session_scope(_TENANT_B) as s:
        rows = set(
            s.execute(
                sql_text("SELECT openai_user_id FROM openai_users")
            ).scalars()
        )
    assert rows == {"user-b"}


def test_user_cascades_on_tenant_delete(clean_state: None) -> None:
    _provision_tenant(_TENANT_A)
    with session_scope(_TENANT_A) as s:
        s.execute(
            sql_text(
                "INSERT INTO openai_users (tenant_id, openai_user_id) "
                "VALUES (:t, :u)"
            ),
            {"t": _TENANT_A, "u": "user-alpha"},
        )
    with engine.begin() as conn:
        conn.execute(
            sql_text("DELETE FROM tenants WHERE tenant_id = :t"),
            {"t": _TENANT_A},
        )
        remaining = conn.execute(
            sql_text(
                "SELECT count(*) FROM openai_users WHERE tenant_id = :t"
            ),
            {"t": _TENANT_A},
        ).scalar_one()
    assert remaining == 0
