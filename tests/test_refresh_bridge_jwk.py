# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM2 Phase C4 — refresh_bridge_jwk Celery task tests.

The task is a thin wrapper around bridge_verifier.fetch_and_cache_jwk()
but with one behavioral difference: it MUST NOT raise on failure.
Rationale: the MCP server is already running with a cached JWK
that's still valid (just possibly stale). A transient gateway hiccup
during the refresh window shouldn't kick Celery into retry-and-fail
territory; the cache stays warm and the next 24h tick re-fetches.

Cases:

  - Successful refresh — task returns {refreshed: True, kid: <new>}
    and the cache reflects the new JWK.
  - Failed refresh — task returns {refreshed: False, reason: ...}
    WITHOUT raising. The previously-cached JWK is preserved.
  - Task is registered in the worker's task registry under the
    canonical name (catches the autodiscovery trap from TM1's
    memory rule).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


VALID_JWK = {
    "kty": "EC",
    "crv": "P-256",
    "x": "qvR2hUiUdV6KMETXQrAfwV8Yi0bSF-ycIxc0zLV5zDQ",
    "y": "N0tKSvkgT772rpGnjAqOZyGWwPztehbfJOGLw4CmvP4",
    "kid": "ogma-bridge-refresh-test",
    "alg": "ES256",
    "use": "sig",
}


@pytest.fixture(autouse=True)
def reset_verifier():
    from mcp_server.auth import bridge_verifier

    bridge_verifier.reset_for_test()
    yield
    bridge_verifier.reset_for_test()


def test_refresh_task_returns_refreshed_true_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path — task wraps the fetcher, returns the new kid."""
    from mcp_server.auth import bridge_verifier
    from mcp_server.tasks.refresh_bridge_jwk import refresh_bridge_jwk

    def fake_fetch(**_kwargs):
        bridge_verifier.set_jwk(VALID_JWK)
        return VALID_JWK

    monkeypatch.setattr(
        bridge_verifier, "fetch_and_cache_jwk", fake_fetch
    )

    result = refresh_bridge_jwk.run()
    assert result == {
        "refreshed": True,
        "kid": "ogma-bridge-refresh-test",
    }
    assert bridge_verifier.cached_jwk()["kid"] == "ogma-bridge-refresh-test"


def test_refresh_task_returns_refreshed_false_on_failure_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure path — the task MUST NOT raise. Caller sees the failure
    via the return dict; the previously-cached JWK is preserved.
    """
    from mcp_server.auth import bridge_verifier
    from mcp_server.tasks.refresh_bridge_jwk import refresh_bridge_jwk

    # Seed a previously-cached JWK so we can verify it's not wiped.
    previous = dict(VALID_JWK)
    previous["kid"] = "previously-cached-kid"
    bridge_verifier.set_jwk(previous)

    import httpx

    def failing_fetch(**_kwargs):
        raise httpx.HTTPStatusError(
            "gateway 503", request=MagicMock(), response=MagicMock()
        )

    monkeypatch.setattr(
        bridge_verifier, "fetch_and_cache_jwk", failing_fetch
    )

    # Critically: this must NOT raise.
    result = refresh_bridge_jwk.run()
    assert result["refreshed"] is False
    assert "HTTPStatusError" in result["reason"]
    # Stale cache survives.
    assert bridge_verifier.cached_jwk()["kid"] == "previously-cached-kid"


def test_task_is_registered_with_canonical_name() -> None:
    """The Celery beat schedule references this exact task name.

    Catches the autodiscovery trap (CLAUDE.md memory): the task
    decorator must run, the module must be imported via
    mcp_server.tasks.__init__, and the canonical name must match
    the beat-schedule entry in vargate_telemetry/celery_app.py.
    """
    from vargate_telemetry.celery_app import celery_app

    canonical = "mcp_server.tasks.refresh_bridge_jwk.refresh_bridge_jwk"
    assert canonical in celery_app.tasks


def test_beat_schedule_entry_exists() -> None:
    """Sanity check that beat picks up the task on its 24h cadence."""
    from vargate_telemetry.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert "refresh-bridge-jwk" in schedule
    entry = schedule["refresh-bridge-jwk"]
    assert entry["task"] == (
        "mcp_server.tasks.refresh_bridge_jwk.refresh_bridge_jwk"
    )
    assert entry["schedule"] == 86400.0
