# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Shared pytest fixtures + test-DB isolation (T5.6 hardening).

CRITICAL: tests must NEVER run against the production database.

Background: this codebase has a single Postgres instance shared
between the live application and the test suite. Multiple test
fixtures execute ``TRUNCATE ... CASCADE`` against ``telemetry_records``,
``tenants``, etc. — running them against the prod DB destroys live
tenant data (which has happened during T5.x sprint work; the founder's
77-record EU tenant got wiped + had to be re-onboarded).

The fix is to redirect every test session to an isolated test
database. This conftest module:

1. **Substitutes the database name** in ``DATABASE_URL`` from
   ``vargate_telemetry`` → ``vargate_telemetry_test`` at module-load
   time, BEFORE any test file imports ``vargate_telemetry.db`` (which
   reads ``DATABASE_URL`` on import to build its singleton ``engine``).
2. **Installs a fail-fast guard**: if the resolved URL after
   substitution still points at the production database name,
   ``RuntimeError`` immediately so the session refuses to run. The
   substitution should always succeed; the guard exists to catch a
   future refactor that breaks the regex.
3. **Self-bootstraps the test DB** in the ``apply_migrations``
   session-scope fixture: connects to the master ``postgres`` database
   on the same server, runs ``CREATE DATABASE`` if the test DB
   doesn't exist yet, then ``alembic upgrade head`` against it.

After this lands, pytest runs are safe regardless of which DB the
production app is bound to. The two databases share the Postgres
server but nothing else — no shared tables, no shared row state,
no possibility of a test TRUNCATE touching the prod side.
"""

from __future__ import annotations

import os
import re

# ───────────────────────────────────────────────────────────────────────────
# Module-load step 1: redirect DATABASE_URL to the test database.
# Runs at conftest module import, which happens before pytest collects
# test files. By the time any test file does
#     from vargate_telemetry.db import engine
# the engine is built against the test DB.
# ───────────────────────────────────────────────────────────────────────────

PROD_DB_NAME = "vargate_telemetry"
TEST_DB_NAME = "vargate_telemetry_test"

_orig_url = os.environ.get("DATABASE_URL", "")
if not _orig_url:
    raise RuntimeError(
        "DATABASE_URL is not set. The test conftest needs it to figure "
        "out the Postgres host + credentials; the actual database name "
        "gets rewritten to the test DB."
    )


def _redirect_to_test_db(url: str) -> str:
    """Replace the trailing ``/{PROD_DB_NAME}`` segment with the test DB.

    Handles both no-query and with-query forms:
      postgresql+psycopg://user:pass@host:5432/vargate_telemetry
      postgresql+psycopg://user:pass@host:5432/vargate_telemetry?sslmode=require

    If the URL already points at the test DB (idempotent re-invoke
    or operator-set override), returns it unchanged.
    """
    if f"/{TEST_DB_NAME}" in url:
        return url  # already redirected
    return re.sub(
        rf"/{re.escape(PROD_DB_NAME)}(\?|$)",
        f"/{TEST_DB_NAME}\\1",
        url,
    )


_test_url = _redirect_to_test_db(_orig_url)

# Fail-fast guard. Substitution should always succeed when DATABASE_URL
# points at the prod DB. If the URL after substitution STILL targets
# the prod DB name (regex failure, unrecognized URL shape, operator
# typo), refuse to run rather than silently TRUNCATE customer data.
#
# Match the prod-DB *segment* exactly — terminator is either
# end-of-string (no query params) or `?` (query params follow). Be
# careful NOT to match the test DB name, which contains the prod DB
# name as a prefix (`vargate_telemetry_test` starts with
# `vargate_telemetry`).
_prod_segment = f"/{PROD_DB_NAME}"
_targets_prod = _test_url.endswith(_prod_segment) or (
    f"{_prod_segment}?" in _test_url
)
if _targets_prod:
    raise RuntimeError(
        "REFUSING TO RUN TESTS: DATABASE_URL would target the "
        f"production database `{PROD_DB_NAME}`. After redirect "
        f"attempt the URL is:\n  {_test_url!r}\n"
        "The conftest substitution failed to redirect to "
        f"`{TEST_DB_NAME}`. Aborting before any TRUNCATE-bearing "
        "fixture can run."
    )

os.environ["DATABASE_URL"] = _test_url


# ───────────────────────────────────────────────────────────────────────────
# Test-DB bootstrap helpers
# ───────────────────────────────────────────────────────────────────────────


def _master_db_url() -> str:
    """Build a URL to the Postgres master DB on the same server.

    CREATE DATABASE can't run from inside the target DB; we have to
    connect to the default ``postgres`` database first.
    """
    return re.sub(
        rf"/{re.escape(TEST_DB_NAME)}(\?|$)",
        "/postgres\\1",
        _test_url,
    )


def _strip_sqlalchemy_dialect(url: str) -> str:
    """``postgresql+psycopg://...`` → ``postgresql://...`` for raw psycopg.

    SQLAlchemy URLs carry a dialect+driver prefix that psycopg's own
    ``connect()`` doesn't understand. Strip the ``+driver`` segment.
    """
    return re.sub(r"^postgresql\+[a-z0-9]+://", "postgresql://", url)


def _ensure_test_db_exists() -> None:
    """Create the test database if it doesn't already exist.

    Idempotent — safe to call on every test session start. CREATE
    DATABASE requires autocommit mode (it can't run inside a
    transaction).
    """
    import psycopg

    master_url = _strip_sqlalchemy_dialect(_master_db_url())
    with psycopg.connect(master_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (TEST_DB_NAME,),
            )
            if cur.fetchone() is None:
                # Use safe quoting — the DB name is from a code
                # constant, but be defensive against future changes.
                cur.execute(
                    f'CREATE DATABASE "{TEST_DB_NAME}"'
                )


# ───────────────────────────────────────────────────────────────────────────
# Now safe to import pytest + define fixtures.
# ───────────────────────────────────────────────────────────────────────────

import pytest  # noqa: E402  — order matters; URL override above must run first


@pytest.fixture(scope="session", autouse=True)
def apply_migrations() -> None:
    """Bootstrap the test DB + bring its schema to alembic's HEAD.

    Runs once per pytest session. Steps:
      1. Ensure ``vargate_telemetry_test`` exists (CREATE DATABASE if
         missing — first run).
      2. ``alembic upgrade head`` against the test DB (no-op on
         subsequent runs since the schema is already at HEAD).

    The TRUNCATE fixtures in individual test files (e.g.,
    ``clean_records``, ``clean_pull_state``) then handle per-test
    state without touching the prod DB.
    """
    _ensure_test_db_exists()

    from alembic import command
    from alembic.config import Config

    # /app/alembic.ini is where the Dockerfile drops it; fall back to
    # the checkout-relative path so this also works on a developer's
    # host if they ever invoke pytest outside the container.
    candidates = ["/app/alembic.ini", "alembic.ini"]
    cfg_path = next((p for p in candidates if os.path.exists(p)), None)
    if cfg_path is None:
        raise RuntimeError(
            f"alembic.ini not found in any of: {candidates!r}; "
            "tests need migrations to be runnable."
        )

    alembic_cfg = Config(cfg_path)
    command.upgrade(alembic_cfg, "head")
