# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Infrastructure smoke tests for T1.4.

These run against the live dev compose stack (postgres + minio + redis +
celery). The expected execution path is

    docker compose run --rm celery-worker pytest tests/test_telemetry_infra.py

so the test process inherits the in-container service hostnames
(`postgres`, `minio`, `redis`).
"""

from __future__ import annotations

import os

import httpx
import pytest
from sqlalchemy import text


def test_postgres_reachable() -> None:
    """Engine accepts a connection and a trivial query round-trips."""
    from vargate_telemetry.db import engine

    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        assert result.scalar() == 1


def test_minio_reachable() -> None:
    """MinIO health-live endpoint returns 200 over the compose network."""
    minio_host = os.environ.get("MINIO_HOST", "minio")
    minio_port = os.environ.get("MINIO_PORT", "9000")
    url = f"http://{minio_host}:{minio_port}/minio/health/live"
    r = httpx.get(url, timeout=5.0)
    assert r.status_code == 200, f"MinIO health-live returned {r.status_code}"


@pytest.mark.skipif(
    not os.environ.get("CELERY_TEST_LIVE"),
    reason="CELERY_TEST_LIVE not set; skipping live-worker round-trip",
)
def test_celery_worker_responsive() -> None:
    """Enqueue add(2, 3); a worker should return 5 within 10s."""
    from vargate_telemetry.tasks.diagnostics import add

    result = add.delay(2, 3)
    assert result.get(timeout=10) == 5


def test_session_scope_rejects_no_tenant() -> None:
    """`session_scope(None)` and `session_scope("")` must raise."""
    from vargate_telemetry.db import session_scope

    with pytest.raises(ValueError):
        with session_scope(None):  # type: ignore[arg-type]
            pass

    with pytest.raises(ValueError):
        with session_scope(""):
            pass
