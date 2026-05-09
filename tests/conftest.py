# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Shared pytest fixtures.

`apply_migrations` ensures every test session starts with the schema at
HEAD, so tests that depend on tables (RLS canary, tenant_deks, etc.)
don't have to call out to alembic themselves.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def apply_migrations() -> None:
    """Bring the dev Postgres schema to alembic's HEAD once per test session."""
    from alembic import command
    from alembic.config import Config

    # /app/alembic.ini is where the Dockerfile drops it; fall back to the
    # checkout-relative path so this also works on a developer's host if
    # they ever invoke pytest outside the container.
    candidates = ["/app/alembic.ini", "alembic.ini"]
    cfg_path = next((p for p in candidates if os.path.exists(p)), None)
    if cfg_path is None:
        raise RuntimeError(
            f"alembic.ini not found in any of: {candidates!r}; "
            "tests need migrations to be runnable."
        )

    alembic_cfg = Config(cfg_path)
    command.upgrade(alembic_cfg, "head")
