# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Postgres engine, sessionmaker, and tenant-scoped session_scope (T1.4).

`session_scope(tenant_id)` is the only blessed way to open a session: it
sets `app.tenant_id` as a transaction-local GUC so the row-level-security
policies enabled in T1.5 onward see the correct tenant. Code that bypasses
session_scope and uses SessionLocal() directly will read zero rows from
RLS-protected tables — that is intentional, not a bug.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

current_tenant: ContextVar[str | None] = ContextVar("current_tenant", default=None)

engine = create_engine(
    os.environ["DATABASE_URL"],
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@contextmanager
def session_scope(tenant_id: str) -> Iterator[Session]:
    """Open a session pinned to a tenant.

    The tenant_id is stored in the transaction-local Postgres GUC
    `app.tenant_id` via `set_config(..., true)`; RLS policies installed
    in T1.5+ use that GUC to filter rows. Passing `None` or an empty
    string is rejected — RLS would otherwise fall back to the unset
    branch and either reveal nothing or, depending on the policy,
    everything. Both modes are bugs we'd rather fail loud than ship.
    """
    if not tenant_id:
        raise ValueError("session_scope requires a non-empty tenant_id")

    token = current_tenant.set(tenant_id)
    s = SessionLocal()
    try:
        # `set_config(name, value, is_local=true)` is the parameter-safe
        # equivalent of `SET LOCAL <name> = <value>` — Postgres does not
        # accept bind parameters in `SET LOCAL`, so we go through the
        # function form instead.
        s.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": tenant_id},
        )
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
        current_tenant.reset(token)
