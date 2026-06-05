# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the TM8 OpenAI projects/keys/users sync (``pull_openai_projects``).

This task does NOT write telemetry_records — it upserts three side
tables (migration 0025) that resolve opaque OpenAI ids to names and,
for users, to the email cross-vendor attribution depends on. The
analogue of ``pull_admin._sync_workspaces`` / ``_sync_api_keys``.

Scenarios:
  - happy path → projects + per-project api_keys + users upserted;
  - upsert is idempotent (second sync updates, doesn't duplicate);
  - 403 on the org-level list → soft-skip dict;
  - the synced ``openai_users`` email is what later lets usage records
    attribute cross-vendor (verified here by reading the side table; the
    end-to-end stitch is covered in test_pull_openai_usage).

The list endpoints route by path: ``/projects``,
``/projects/{id}/api_keys``, ``/users`` — each ``{object:"list", data,
first_id, last_id, has_more}``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterator

import httpx
import pytest
from sqlalchemy import text as sql_text

from vargate_telemetry.openai import OpenAIAdminClient

_CREATED = int(datetime(2024, 7, 22, 18, 10, 6, tzinfo=timezone.utc).timestamp())
_LAST_USED = int(datetime(2026, 6, 1, 9, tzinfo=timezone.utc).timestamp())


def _list(data: list[dict]) -> dict:
    return {
        "object": "list",
        "data": data,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
        "has_more": False,
    }


def _project(pid: str, name: str = "Alpha", status: str = "active") -> dict:
    return {
        "id": pid,
        "object": "organization.project",
        "name": name,
        "status": status,
        "created_at": _CREATED,
        "archived_at": None,
    }


def _api_key(kid: str, name: str = "CI key") -> dict:
    return {
        "id": kid,
        "object": "organization.project.api_key",
        "name": name,
        "created_at": _CREATED,
        "last_used_at": _LAST_USED,
        "owner": {
            "type": "user",
            "user": {"id": "user-alice", "email": "alice@example.com"},
        },
        "owner_project_access": "active",
        "redacted_value": "sk-proj-****ABCD",
    }


def _user(uid: str, email: str, name: str = "Alice", role: str = "owner") -> dict:
    return {
        "id": uid,
        "object": "organization.user",
        "email": email,
        "name": name,
        "role": role,
        "added_at": _CREATED,
    }


def _routing_handler(
    *,
    projects: list[dict],
    api_keys_by_project: dict[str, list[dict]],
    users: list[dict],
) -> Callable[[httpx.Request], httpx.Response]:
    """Route GET by path across the three list endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/projects"):
            return httpx.Response(200, json=_list(projects))
        if path.endswith("/api_keys"):
            # /v1/organization/projects/{id}/api_keys
            project_id = path.split("/projects/")[1].split("/api_keys")[0]
            return httpx.Response(
                200, json=_list(api_keys_by_project.get(project_id, []))
            )
        if path.endswith("/users"):
            return httpx.Response(200, json=_list(users))
        return httpx.Response(404, json={"error": {"message": path}})

    return handler


def _stub_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> OpenAIAdminClient:
    return OpenAIAdminClient(
        api_key="sk-admin-test",
        base_url="https://api.test/v1/organization",
        min_wait=0.0,
        max_wait=0.0,
        wait_multiplier=0.0,
        transport=httpx.MockTransport(handler),
    )


_OPENAI_TABLES = ("openai_projects", "openai_api_keys", "openai_users")


@pytest.fixture
def clean_state() -> Iterator[None]:
    from vargate_telemetry.db import engine

    def _truncate() -> None:
        with engine.begin() as conn:
            for table in _OPENAI_TABLES:
                conn.execute(
                    sql_text(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
                )
            conn.execute(
                sql_text(
                    "DELETE FROM tenants WHERE tenant_id LIKE 'tnt_us_oai_proj%'"
                )
            )

    _truncate()
    yield
    _truncate()


def _provision_tenant(tenant_id: str) -> None:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "billing_status) VALUES (:t, 'us', TRUE, 'trial') "
                "ON CONFLICT (tenant_id) DO NOTHING"
            ),
            {"t": tenant_id},
        )


# ───────────────────────────────────────────────────────────────────────────
# Happy path — all three side tables populated
# ───────────────────────────────────────────────────────────────────────────


def test_pull_openai_projects_syncs_all_three_tables(
    clean_state: None,
) -> None:
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_projects import (
        _pull_openai_projects_for_tenant,
    )

    tenant = "tnt_us_oai_proj_all"
    _provision_tenant(tenant)

    handler = _routing_handler(
        projects=[_project("proj_alpha", "Alpha"), _project("proj_beta", "Beta")],
        api_keys_by_project={
            "proj_alpha": [_api_key("key_a", "Alpha CI")],
            "proj_beta": [_api_key("key_b1"), _api_key("key_b2")],
        },
        users=[
            _user("user-alice", "alice@example.com"),
            _user("user-bob", "bob@example.com", name="Bob", role="reader"),
        ],
    )
    result = _pull_openai_projects_for_tenant(
        tenant, client=_stub_client(handler)
    )
    assert result["status"] == "ok"
    assert result["projects"] == 2
    assert result["api_keys"] == 3  # 1 + 2
    assert result["users"] == 2

    with session_scope(tenant) as s:
        projects = {
            r.project_id: r.name
            for r in s.execute(
                sql_text(
                    "SELECT project_id, name FROM openai_projects "
                    "WHERE tenant_id = :t"
                ),
                {"t": tenant},
            ).all()
        }
        keys = {
            r.api_key_id: (r.project_id, r.name)
            for r in s.execute(
                sql_text(
                    "SELECT api_key_id, project_id, name "
                    "FROM openai_api_keys WHERE tenant_id = :t"
                ),
                {"t": tenant},
            ).all()
        }
        users = {
            r.openai_user_id: (r.email, r.role)
            for r in s.execute(
                sql_text(
                    "SELECT openai_user_id, email, role "
                    "FROM openai_users WHERE tenant_id = :t"
                ),
                {"t": tenant},
            ).all()
        }

    assert projects == {"proj_alpha": "Alpha", "proj_beta": "Beta"}
    assert keys == {
        "key_a": ("proj_alpha", "Alpha CI"),
        "key_b1": ("proj_beta", "CI key"),
        "key_b2": ("proj_beta", "CI key"),
    }
    # The email is the cross-vendor match key — must land verbatim.
    assert users == {
        "user-alice": ("alice@example.com", "owner"),
        "user-bob": ("bob@example.com", "reader"),
    }


def test_pull_openai_projects_upsert_is_idempotent(
    clean_state: None,
) -> None:
    """A second sync of the same org updates names/status in place — no
    duplicate rows (the ON CONFLICT DO UPDATE posture). A project
    renamed between syncs reflects the new name."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_projects import (
        _pull_openai_projects_for_tenant,
    )

    tenant = "tnt_us_oai_proj_idem"
    _provision_tenant(tenant)

    first = _routing_handler(
        projects=[_project("proj_alpha", "Alpha", "active")],
        api_keys_by_project={"proj_alpha": [_api_key("key_a", "Old name")]},
        users=[_user("user-alice", "alice@example.com")],
    )
    _pull_openai_projects_for_tenant(tenant, client=_stub_client(first))

    # Re-sync: project renamed + archived, key renamed, user role changed.
    second = _routing_handler(
        projects=[_project("proj_alpha", "Alpha Renamed", "archived")],
        api_keys_by_project={"proj_alpha": [_api_key("key_a", "New name")]},
        users=[
            _user("user-alice", "alice@example.com", role="reader"),
        ],
    )
    result = _pull_openai_projects_for_tenant(
        tenant, client=_stub_client(second)
    )
    assert result["status"] == "ok"

    with session_scope(tenant) as s:
        proj_count = s.execute(
            sql_text(
                "SELECT count(*) FROM openai_projects WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).scalar_one()
        proj = s.execute(
            sql_text(
                "SELECT name, status FROM openai_projects "
                "WHERE tenant_id = :t AND project_id = 'proj_alpha'"
            ),
            {"t": tenant},
        ).one()
        key_name = s.execute(
            sql_text(
                "SELECT name FROM openai_api_keys "
                "WHERE tenant_id = :t AND api_key_id = 'key_a'"
            ),
            {"t": tenant},
        ).scalar_one()
        role = s.execute(
            sql_text(
                "SELECT role FROM openai_users "
                "WHERE tenant_id = :t AND openai_user_id = 'user-alice'"
            ),
            {"t": tenant},
        ).scalar_one()

    assert proj_count == 1  # updated in place, not duplicated
    assert proj.name == "Alpha Renamed"
    assert proj.status == "archived"
    assert key_name == "New name"
    assert role == "reader"


def test_pull_openai_projects_name_falls_back_to_id(
    clean_state: None,
) -> None:
    """``openai_projects.name`` is NOT NULL; a project the vendor returns
    without a name falls back to the id so the upsert doesn't violate the
    constraint."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_projects import (
        _pull_openai_projects_for_tenant,
    )

    tenant = "tnt_us_oai_proj_noname"
    _provision_tenant(tenant)

    proj = _project("proj_noname")
    proj["name"] = None  # vendor omitted the name
    handler = _routing_handler(
        projects=[proj],
        api_keys_by_project={},
        users=[],
    )
    result = _pull_openai_projects_for_tenant(
        tenant, client=_stub_client(handler)
    )
    assert result["status"] == "ok"

    with session_scope(tenant) as s:
        name = s.execute(
            sql_text(
                "SELECT name FROM openai_projects "
                "WHERE tenant_id = :t AND project_id = 'proj_noname'"
            ),
            {"t": tenant},
        ).scalar_one()
    assert name == "proj_noname"


# ───────────────────────────────────────────────────────────────────────────
# 403 soft-skip on the org-level list
# ───────────────────────────────────────────────────────────────────────────


def test_pull_openai_projects_skips_when_403(clean_state: None) -> None:
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_projects import (
        _pull_openai_projects_for_tenant,
    )

    tenant = "tnt_us_oai_proj_403"
    _provision_tenant(tenant)

    def handler_403(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "no"}})

    result = _pull_openai_projects_for_tenant(
        tenant, client=_stub_client(handler_403)
    )
    assert result["status"] == "no_openai_projects_access"
    assert result["projects"] == 0
    assert result["api_keys"] == 0
    assert result["users"] == 0

    with session_scope(tenant) as s:
        count = s.execute(
            sql_text(
                "SELECT count(*) FROM openai_projects WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).scalar_one()
    assert count == 0


def test_pull_openai_projects_skips_inaccessible_project_keys(
    clean_state: None,
) -> None:
    """A 403 on ONE project's api_keys endpoint is skipped (logged), but
    the rest of the sync — other projects' keys + users — still
    completes. Org-level lists succeeded; only that project's keys are
    denied."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_projects import (
        _pull_openai_projects_for_tenant,
    )

    tenant = "tnt_us_oai_proj_partial"
    _provision_tenant(tenant)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/projects"):
            return httpx.Response(
                200, json=_list([_project("proj_ok"), _project("proj_denied")])
            )
        if path.endswith("/api_keys"):
            project_id = path.split("/projects/")[1].split("/api_keys")[0]
            if project_id == "proj_denied":
                return httpx.Response(403, json={"error": {"message": "no"}})
            return httpx.Response(200, json=_list([_api_key("key_ok")]))
        if path.endswith("/users"):
            return httpx.Response(
                200, json=_list([_user("user-alice", "alice@example.com")])
            )
        return httpx.Response(404, json={})

    result = _pull_openai_projects_for_tenant(
        tenant, client=_stub_client(handler)
    )
    # Both projects synced, only the accessible project's key synced,
    # users synced — the single-project 403 didn't abort the run.
    assert result["status"] == "ok"
    assert result["projects"] == 2
    assert result["api_keys"] == 1
    assert result["users"] == 1

    with session_scope(tenant) as s:
        key_ids = set(
            s.execute(
                sql_text(
                    "SELECT api_key_id FROM openai_api_keys "
                    "WHERE tenant_id = :t"
                ),
                {"t": tenant},
            ).scalars()
        )
    assert key_ids == {"key_ok"}


# ───────────────────────────────────────────────────────────────────────────
# Dispatcher
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def dispatch_tenants() -> Iterator[dict]:
    import uuid as _uuid

    from vargate_telemetry.db import engine

    sfx = _uuid.uuid4().hex[:8]
    ids = {
        "us_active": f"t-oaip-us-{sfx}",
        "eu_active": f"t-oaip-eu-{sfx}",
        "us_inactive": f"t-oaip-ui-{sfx}",
    }
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "billing_status) VALUES "
                "(:ua, 'us', true, 'paying'), "
                "(:ea, 'eu', true, 'paying'), "
                "(:ui, 'us', false, 'cancelled')"
            ),
            {
                "ua": ids["us_active"],
                "ea": ids["eu_active"],
                "ui": ids["us_inactive"],
            },
        )
    yield ids
    with engine.begin() as conn:
        conn.execute(
            sql_text("DELETE FROM tenants WHERE tenant_id = ANY(:ids)"),
            {"ids": list(ids.values())},
        )


def test_dispatch_openai_projects_default_all_regions(
    dispatch_tenants: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vargate_telemetry.tasks import pull_openai_projects

    dispatched: list[str] = []
    monkeypatch.setattr(
        pull_openai_projects.pull_openai_projects_for_tenant,
        "delay",
        lambda tenant_id: dispatched.append(tenant_id),
    )
    pull_openai_projects.dispatch_openai_projects_pulls()
    ds = set(dispatched)
    assert {
        dispatch_tenants["us_active"],
        dispatch_tenants["eu_active"],
    } <= ds
    assert dispatch_tenants["us_inactive"] not in ds


def test_dispatch_openai_projects_explicit_region_filters(
    dispatch_tenants: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vargate_telemetry.tasks import pull_openai_projects

    dispatched: list[str] = []
    monkeypatch.setattr(
        pull_openai_projects.pull_openai_projects_for_tenant,
        "delay",
        lambda tenant_id: dispatched.append(tenant_id),
    )
    pull_openai_projects.dispatch_openai_projects_pulls(region="eu")
    ds = set(dispatched)
    assert dispatch_tenants["eu_active"] in ds
    assert dispatch_tenants["us_active"] not in ds
