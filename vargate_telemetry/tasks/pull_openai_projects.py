# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""OpenAI Admin projects / api_keys / users sync task (TM8 Phase B).

The OpenAI analogue of ``pull_admin._sync_workspaces`` /
``_sync_api_keys``: this task does NOT write ``telemetry_records`` and
keeps NO cursor. It refreshes three side tables (migration 0025) that
resolve opaque OpenAI ids to human-friendly names — and, for users, to
the **email** that cross-vendor attribution depends on:

  - ``openai_projects`` ← ``GET /projects``
  - ``openai_api_keys``  ← ``GET /projects/{id}/api_keys`` (per project)
  - ``openai_users``     ← ``GET /users``  (carries the PII email)

Why this matters for attribution
================================

``pull_openai_usage`` resolves a usage row's ``user_id`` → email via
``openai_users`` so the alias reconciler can email-match the OpenAI
user to an Ogma ``users`` row. That resolution is only as good as this
table — so this sync should run on the same cadence (beat) as the usage
pull (the Wire stage schedules it).

UPSERT, never delete
====================

Every row is upserted via raw ``INSERT ... ON CONFLICT ... DO UPDATE``
(no ORM model — these are raw-SQL-only side tables, exactly like the
Anthropic ``workspaces`` / ``api_keys`` tables). Idempotent; a project /
key / user that disappears from the org keeps its last-known row so
historical telemetry stays resolvable. Same posture as
``_sync_workspaces``.

Two Celery tasks:

  - ``dispatch_openai_projects_pulls`` — beat fan-out over active tenants.
  - ``pull_openai_projects_for_tenant`` — per-tenant sync.

403 soft-skip: an org tier (or scope-limited key) can 403 on the list
endpoints; ``InsufficientScope`` is caught and returned as
``status="no_openai_projects_access"`` rather than raised.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text as sql_text

from vargate_telemetry.celery_app import celery_app
from vargate_telemetry.db import scheduler_session_scope, session_scope
from vargate_telemetry.openai import (
    InsufficientScope,
    OpenAIAdminClient,
    admin_client_for_tenant,
)

_log = logging.getLogger(__name__)


def _sync_projects(
    tenant_id: str, client: OpenAIAdminClient
) -> list[str]:
    """Upsert this tenant's projects; return the list of project ids.

    The returned ids drive the per-project api_keys sweep. Raises
    ``InsufficientScope`` straight through (the caller turns it into the
    soft-skip dict) — a 403 on ``/projects`` means we can't enumerate
    keys either.
    """
    rows = list(client.list_projects())
    if not rows:
        return []

    project_ids: list[str] = []
    with session_scope(tenant_id) as s:
        for p in rows:
            project_ids.append(p.id)
            s.execute(
                sql_text(
                    """
                    INSERT INTO openai_projects
                        (tenant_id, project_id, name, status,
                         created_at_openai)
                    VALUES (:tenant_id, :project_id, :name, :status,
                            :created)
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
                    "project_id": p.id,
                    # name is NOT NULL in the schema; fall back to the
                    # id when the vendor omits it.
                    "name": p.name or p.id,
                    "status": p.status,
                    "created": p.created_at,
                },
            )
    return project_ids


def _sync_api_keys(
    tenant_id: str, client: OpenAIAdminClient, project_ids: list[str]
) -> int:
    """Upsert api keys for each project; return the count synced.

    Per-project ``GET /projects/{id}/api_keys``. A 403 on a single
    project is logged and skipped (some projects may be inaccessible
    even when the org-level list succeeds); other exceptions propagate.
    """
    synced = 0
    for project_id in project_ids:
        try:
            keys = list(client.list_project_api_keys(project_id))
        except InsufficientScope:
            _log.info(
                "pull_openai_projects: 403 listing keys for project "
                "%s/%s — skipping",
                tenant_id,
                project_id,
            )
            continue

        if not keys:
            continue

        with session_scope(tenant_id) as s:
            for k in keys:
                s.execute(
                    sql_text(
                        """
                        INSERT INTO openai_api_keys
                            (tenant_id, api_key_id, project_id, name,
                             created_at_openai, last_used_at)
                        VALUES (:tenant_id, :api_key_id, :project_id,
                                :name, :created, :last_used)
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
                        "tenant_id": tenant_id,
                        "api_key_id": k.id,
                        "project_id": project_id,
                        "name": k.name,
                        "created": k.created_at,
                        "last_used": k.last_used_at,
                    },
                )
                synced += 1
    return synced


def _sync_users(tenant_id: str, client: OpenAIAdminClient) -> int:
    """Upsert org users (with email) into ``openai_users``; return count.

    The email column is the cross-vendor alias match key — this is the
    load-bearing sync for attribution. Raises ``InsufficientScope``
    through to the caller's soft-skip.
    """
    rows = list(client.list_users())
    if not rows:
        return 0

    with session_scope(tenant_id) as s:
        for u in rows:
            s.execute(
                sql_text(
                    """
                    INSERT INTO openai_users
                        (tenant_id, openai_user_id, email, name, role)
                    VALUES (:tenant_id, :openai_user_id, :email, :name,
                            :role)
                    ON CONFLICT (tenant_id, openai_user_id)
                    DO UPDATE SET
                        email = EXCLUDED.email,
                        name = EXCLUDED.name,
                        role = EXCLUDED.role,
                        synced_at = now()
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "openai_user_id": u.id,
                    "email": u.email,
                    "name": u.name,
                    "role": u.role,
                },
            )
    return len(rows)


def _pull_openai_projects_for_tenant(
    tenant_id: str,
    *,
    client: Optional[OpenAIAdminClient] = None,
) -> dict[str, Any]:
    """Pure-Python sync of projects + api_keys + users. Returns counts.

    Happy path::

        {"projects": P, "api_keys": K, "users": U, "status": "ok"}

    403 soft-skip (org-level list denied)::

        {"projects": 0, "api_keys": 0, "users": 0,
         "status": "no_openai_projects_access"}
    """
    if not tenant_id:
        raise ValueError("tenant_id required")

    owned_client = client is None
    if owned_client:
        try:
            client = admin_client_for_tenant(tenant_id)
        except LookupError:
            # No OpenAI admin key sealed — soft-skip (the dispatcher fans
            # out to ALL active tenants; most have no OpenAI key). No retry.
            _log.debug(
                "pull_openai_projects: no openai key sealed for %s",
                tenant_id,
            )
            return {
                "projects": 0,
                "api_keys": 0,
                "users": 0,
                "status": "no_openai_key",
            }

    try:
        try:
            project_ids = _sync_projects(tenant_id, client)
            api_keys = _sync_api_keys(tenant_id, client, project_ids)
            users = _sync_users(tenant_id, client)
        except InsufficientScope:
            _log.info(
                "pull_openai_projects: 403 no_openai_projects_access "
                "for %s",
                tenant_id,
            )
            return {
                "projects": 0,
                "api_keys": 0,
                "users": 0,
                "status": "no_openai_projects_access",
            }
    finally:
        if owned_client:
            client.close()

    return {
        "projects": len(project_ids),
        "api_keys": api_keys,
        "users": users,
        "status": "ok",
    }


@celery_app.task(
    bind=True,
    max_retries=3,
    name=(
        "vargate_telemetry.tasks.pull_openai_projects."
        "pull_openai_projects_for_tenant"
    ),
)
def pull_openai_projects_for_tenant(self, tenant_id: str) -> dict[str, Any]:
    """Beat-dispatched per-tenant projects/keys/users sync. Retries on
    any exception OTHER than the 403 soft-skip (which returns cleanly)."""
    try:
        return _pull_openai_projects_for_tenant(tenant_id)
    except Exception as exc:
        _log.exception("pull_openai_projects failed for %s", tenant_id)
        raise self.retry(exc=exc, countdown=120)


@celery_app.task(
    name=(
        "vargate_telemetry.tasks.pull_openai_projects."
        "dispatch_openai_projects_pulls"
    ),
)
def dispatch_openai_projects_pulls(region: Optional[str] = None) -> int:
    """Beat fan-out. Enumerate active tenants; queue one sync each.

    Mirrors ``pull_admin.dispatch_admin_pulls`` — all regions by
    default (TM5 T5.0 region-gap fix), 403 soft-skip in the per-tenant
    task.
    """
    with scheduler_session_scope() as s:
        if region is None:
            rows = s.execute(
                sql_text(
                    "SELECT tenant_id FROM tenants WHERE active = true"
                )
            ).all()
        else:
            rows = s.execute(
                sql_text(
                    "SELECT tenant_id FROM tenants "
                    "WHERE active = true AND region = :r"
                ),
                {"r": region},
            ).all()

    for row in rows:
        pull_openai_projects_for_tenant.delay(row.tenant_id)

    _log.info(
        "dispatch_openai_projects_pulls: queued %d tenants in region %s",
        len(rows),
        region or "all",
    )
    return len(rows)


__all__ = [
    "_pull_openai_projects_for_tenant",
    "dispatch_openai_projects_pulls",
    "pull_openai_projects_for_tenant",
]
