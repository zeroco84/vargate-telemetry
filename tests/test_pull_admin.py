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

from fixtures.admin_api_handlers import (
    empty_api_keys_response,
    empty_workspaces_response,
    is_api_keys_request,
    is_workspaces_request,
    skip_workspaces,
)
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


@skip_workspaces
def _two_bucket_handler(
    request: httpx.Request,
) -> httpx.Response:
    """Returns two fixed-timestamp UsageBucket rows regardless of the window.

    ``@skip_workspaces`` shunts the T5.5.6 workspace-sync call to the
    empty-envelope response so this handler only ever sees usage
    requests.
    """
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
        # the workspace sync's. TM3 Phase A4 adds the api_keys
        # sync — same short-circuit applies.
        if is_workspaces_request(request):
            return empty_workspaces_response()
        if is_api_keys_request(request):
            return empty_api_keys_response()
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


@pytest.fixture
def dispatch_tenants() -> Iterator[dict]:
    """Unique-id tenants (us+eu active, us inactive) for the dispatcher
    tests; scoped DELETE teardown. Subset/disjoint assertions keep this
    immune to other tests' tenants in the shared DB — never a global
    `TRUNCATE tenants` (that cascade-wipes other tests; it broke t2)."""
    import uuid as _uuid

    from vargate_telemetry.db import engine

    sfx = _uuid.uuid4().hex[:8]
    ids = {
        "us_active": f"t-padisp-us-{sfx}",
        "eu_active": f"t-padisp-eu-{sfx}",
        "us_inactive": f"t-padisp-ui-{sfx}",
    }
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, billing_status) "
                "VALUES (:ua, 'us', true, 'paying'), "
                "(:ea, 'eu', true, 'paying'), (:ui, 'us', false, 'cancelled')"
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


def test_dispatch_default_dispatches_all_active_regardless_of_region(
    dispatch_tenants: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TM5 T5.0: with no region arg (how beat calls it), every active
    tenant is dispatched regardless of region. Subset assertion — other
    tests' tenants may also be present in the shared DB."""
    from vargate_telemetry.tasks import pull_admin

    dispatched: list[str] = []
    monkeypatch.setattr(
        pull_admin.pull_admin_for_tenant,
        "delay",
        lambda tenant_id: dispatched.append(tenant_id),
    )

    pull_admin.dispatch_admin_pulls()

    ds = set(dispatched)
    assert {dispatch_tenants["us_active"], dispatch_tenants["eu_active"]} <= ds
    assert dispatch_tenants["us_inactive"] not in ds


def test_dispatch_explicit_region_still_filters(
    dispatch_tenants: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit region arg keeps the filtered behavior: only active
    tenants in that region get dispatched (eu excluded)."""
    from vargate_telemetry.tasks import pull_admin

    dispatched: list[str] = []
    monkeypatch.setattr(
        pull_admin.pull_admin_for_tenant,
        "delay",
        lambda tenant_id: dispatched.append(tenant_id),
    )

    pull_admin.dispatch_admin_pulls(region="us")

    ds = set(dispatched)
    assert dispatch_tenants["us_active"] in ds
    assert dispatch_tenants["eu_active"] not in ds
