# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Lightweight tenant role gating (TM4).

Two roles live on ``users.role``: ``'admin'`` and ``'member'``.

- **admin** may write budgets, map identities to users (alias
  stitching), and change other users' roles.
- **member** is read + self-service only (it can still view everything,
  acknowledge its own alerts, and run onboarding/region-select).

Roles are looked up **fresh from the DB per request** rather than carried
in the JWT, so a promote/demote takes effect on the next call without
forcing the affected user to sign in again. ``users`` has no RLS (see
``models/users.py``), so every query here scopes by ``tenant_id``
explicitly.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, status
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from vargate_telemetry.auth.middleware import AuthenticatedUser, current_user
from vargate_telemetry.db import session_scope

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
VALID_ROLES = frozenset({ROLE_ADMIN, ROLE_MEMBER})


def get_role(
    session: Session, user_id: str, tenant_id: str
) -> Optional[str]:
    """Return the user's role within ``tenant_id``, or None if no such row.

    None means the user is not a member of this tenant — callers treat
    that as "not an admin".
    """
    # Compare on id::text so a malformed (non-UUID) JWT sub resolves to
    # "no such user" (role None / not admin) instead of a DataError 500.
    # Production subs are always canonical UUID strings, which match.
    row = session.execute(
        sql_text(
            "SELECT role FROM users "
            "WHERE id::text = :uid AND tenant_id = :tid"
        ),
        {"uid": user_id, "tid": tenant_id},
    ).first()
    return row.role if row else None


def count_admins(session: Session, tenant_id: str) -> int:
    """How many admins the tenant has — guards against last-admin demotion."""
    return int(
        session.execute(
            sql_text(
                "SELECT count(*) FROM users "
                "WHERE tenant_id = :tid AND role = :admin"
            ),
            {"tid": tenant_id, "admin": ROLE_ADMIN},
        ).scalar()
        or 0
    )


def require_admin(
    user: AuthenticatedUser = Depends(current_user),
) -> AuthenticatedUser:
    """FastAPI dependency: 403 unless the caller is an admin of their tenant.

    Layers on ``current_user`` (401 if unauthenticated). Requires a bound
    tenant (400 if none) and an ``'admin'`` role (403 otherwise). Returns
    the ``AuthenticatedUser`` so handlers can use it exactly as they would
    with ``Depends(current_user)``.
    """
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "no_tenant_bound",
                "message": "Your session is not bound to a tenant yet.",
            },
        )

    with session_scope(user.tenant_id) as session:
        role = get_role(session, user.user_id, user.tenant_id)

    if role != ROLE_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "admin_required",
                "message": (
                    "This action requires an admin role. Ask a tenant "
                    "admin to make the change or to grant you admin."
                ),
            },
        )
    return user
