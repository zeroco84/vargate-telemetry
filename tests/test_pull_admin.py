# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the scheduled Anthropic Admin pull task (T3.5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
import pytest
from sqlalchemy import text as sql_text

from vargate_telemetry.anthropic import AnthropicAdminClient


_FIXED_BUCKET_START = datetime(2026, 5, 9, 0, 0, 0, tzinfo=timezone.utc)
_FIXED_BUCKET_END = _FIXED_BUCKET_START + timedelta(days=1)


def _stub_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AnthropicAdminClient:
    """AnthropicAdminClient wired with MockTransport + zero retry-wait."""
    return AnthropicAdminClient(
        api_key="test-key",
        base_url="https://api.test",
        min_wait=0.0,
        transport=httpx.MockTransport(handler),
    )


def _two_bucket_handler(
    request: httpx.Request,
) -> httpx.Response:
    """Returns two fixed-timestamp UsageBucket rows regardless of the window."""
    # T5.5.6: pull_admin also calls /v1/organizations/workspaces. Short-
    # circuit so the test's bucket-count assertions still apply.
    if "/workspaces" in request.url.path:
        return httpx.Response(200, json={"data": [], "has_more": False})
    return httpx.Response(
        200,
        json={
            "data": [
                {
                    "starting_at": _FIXED_BUCKET_START.isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "ending_at": _FIXED_BUCKET_END.isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "results": [
                        {
                            "model": "claude-sonnet-4-6",
                            "uncached_input_tokens": 1000,
                            "output_tokens": 400,
                        }
                    ],
                },
                {
                    "starting_at": _FIXED_BUCKET_END.isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "ending_at": (
                        _FIXED_BUCKET_END + timedelta(days=1)
                    )
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "results": [
                        {
                            "model": "claude-opus-4-7",
                            "uncached_input_tokens": 5000,
                            "output_tokens": 1500,
                        }
                    ],
                },
            ],
            "has_more": False,
        },
    )


@pytest.fixture
def clean_pull_state() -> None:
    """Empty every pull-touched table + Redis meter keys."""
    from vargate_telemetry.billing import set_dispatcher_for_test
    from vargate_telemetry.db import engine
    from vargate_telemetry.metering import _redis

    r = _redis()
    for key in r.scan_iter("vargate:meter:*"):
        r.delete(key)

    truncate_sql = (
        "TRUNCATE TABLE telemetry_records, usage_records, "
        "pull_state, tenants, billing_retry, tenant_billing "
        "RESTART IDENTITY CASCADE"
    )
    with engine.begin() as conn:
        conn.execute(sql_text(truncate_sql))

    set_dispatcher_for_test(None)

    yield

    for key in r.scan_iter("vargate:meter:*"):
        r.delete(key)
    with engine.begin() as conn:
        conn.execute(sql_text(truncate_sql))
    set_dispatcher_for_test(None)


# -------------------------- pull_admin_for_tenant ------------------------


def test_pull_idempotent(clean_pull_state: None) -> None:
    """Second pull over the same window inserts zero new records."""
    from vargate_telemetry.tasks.pull_admin import _pull_admin_for_tenant

    tenant = "test-pull-idempotent"

    first = _pull_admin_for_tenant(tenant, client=_stub_client(_two_bucket_handler))
    assert first == {"inserted": 2, "deduped": 0}

    # Second invocation: handler returns the SAME bucket timestamps,
    # so the (tenant, source_api, external_id) UNIQUE constraint
    # forces dedup on every row.
    second = _pull_admin_for_tenant(
        tenant, client=_stub_client(_two_bucket_handler)
    )
    assert second == {"inserted": 0, "deduped": 2}


def test_pull_advances_cursor(clean_pull_state: None) -> None:
    """A successful pull writes a cursor at-or-after the pull-start time."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_admin import (
        SOURCE_API_ADMIN,
        _pull_admin_for_tenant,
    )

    tenant = "test-pull-cursor"
    before = datetime.now(timezone.utc)

    _pull_admin_for_tenant(tenant, client=_stub_client(_two_bucket_handler))

    with session_scope(tenant) as s:
        row = s.execute(
            sql_text(
                "SELECT cursor, last_status FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :sa"
            ),
            {"t": tenant, "sa": SOURCE_API_ADMIN},
        ).first()

    assert row is not None
    assert row.last_status == "ok"
    cursor = datetime.fromisoformat(row.cursor)
    assert cursor >= before, (
        f"cursor {cursor} did not advance past pull start {before}"
    )


def test_pull_handles_rate_limit_and_retries(
    clean_pull_state: None,
) -> None:
    """429 → 429 → 200 within one call; the client's tenacity loop hides it."""
    from vargate_telemetry.tasks.pull_admin import _pull_admin_for_tenant

    tenant = "test-pull-rate-limited"
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        # T5.5.6: skip the workspaces sync call from the count so
        # the 429-retry test pins the USAGE call's behavior, not
        # the workspace sync's.
        if "/workspaces" in request.url.path:
            return httpx.Response(
                200, json={"data": [], "has_more": False}
            )
        call_count["n"] += 1
        if call_count["n"] < 3:
            return httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={"error": "rate limited"},
            )
        return _two_bucket_handler(request)

    result = _pull_admin_for_tenant(tenant, client=_stub_client(handler))

    assert result["inserted"] == 2
    assert call_count["n"] == 3  # two 429s + one success


def test_pull_isolated_per_tenant(clean_pull_state: None) -> None:
    """Tenant A's pull never plants rows under tenant B's tenant_id."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_admin import _pull_admin_for_tenant

    tenant_a = "test-pull-iso-A"
    tenant_b = "test-pull-iso-B"

    _pull_admin_for_tenant(
        tenant_a, client=_stub_client(_two_bucket_handler)
    )
    _pull_admin_for_tenant(
        tenant_b, client=_stub_client(_two_bucket_handler)
    )

    with session_scope(tenant_a) as s:
        a_rows = s.execute(
            sql_text(
                "SELECT COUNT(*) FROM telemetry_records "
                "WHERE tenant_id = :t"
            ),
            {"t": tenant_a},
        ).scalar()
    with session_scope(tenant_b) as s:
        b_rows = s.execute(
            sql_text(
                "SELECT COUNT(*) FROM telemetry_records "
                "WHERE tenant_id = :t"
            ),
            {"t": tenant_b},
        ).scalar()
        # Tenant B's RLS scope: should see 2 of its own rows, ZERO of A's.
        b_can_see_a = s.execute(
            sql_text(
                "SELECT COUNT(*) FROM telemetry_records "
                "WHERE tenant_id = :a"
            ),
            {"a": tenant_a},
        ).scalar()

    assert a_rows == 2
    assert b_rows == 2
    assert b_can_see_a == 0


# -------------------------- dispatch_admin_pulls -------------------------


def test_dispatch_filters_active_and_region(
    clean_pull_state: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only active tenants in the requested region get dispatched."""
    from vargate_telemetry.db import engine
    from vargate_telemetry.tasks import pull_admin

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, billing_status) "
                "VALUES "
                "('t-us-active',   'us', true,  'paying'), "
                "('t-us-inactive', 'us', false, 'cancelled'), "
                "('t-eu-active',   'eu', true,  'paying')"
            )
        )

    dispatched: list[str] = []
    monkeypatch.setattr(
        pull_admin.pull_admin_for_tenant,
        "delay",
        lambda tenant_id: dispatched.append(tenant_id),
    )

    count = pull_admin.dispatch_admin_pulls(region="us")

    assert count == 1
    assert dispatched == ["t-us-active"]
