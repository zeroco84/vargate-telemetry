# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the chunked, resumable Admin API backfill (T3.6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text as sql_text

from fixtures.admin_api_handlers import (
    empty_workspaces_response,
    is_workspaces_request,
)
from vargate_telemetry.anthropic import AnthropicAdminClient


def _stub_client(handler) -> AnthropicAdminClient:
    return AnthropicAdminClient(
        api_key="test-key",
        base_url="https://api.test",
        min_wait=0.0,
        transport=httpx.MockTransport(handler),
    )


def _empty_data_handler(request: httpx.Request) -> httpx.Response:
    """Returns a no-data envelope. Each chunk request succeeds with zero rows."""
    return httpx.Response(
        200,
        json={"data": [], "has_more": False},
    )


@pytest.fixture
def clean_backfill_state() -> None:
    """Empty every backfill-touched table + Redis meter keys."""
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


def test_backfill_chunks_respect_window(clean_backfill_state: None) -> None:
    """21 days / 7-day chunks → 3 contiguous 7-day windows."""
    from vargate_telemetry.tasks.pull_admin import (
        _backfill_admin_for_tenant,
    )

    tenant = "test-backfill-chunks"
    seen_windows: list[tuple[str, str]] = []

    def tracking_handler(request: httpx.Request) -> httpx.Response:
        # T5.5.6: backfill also hits /v1/organizations/workspaces.
        # Don't count those against the 7-day-window tracker —
        # `seen_windows` pins the usage-endpoint windows only.
        if is_workspaces_request(request):
            return empty_workspaces_response()
        seen_windows.append(
            (
                request.url.params["starting_at"],
                request.url.params["ending_at"],
            )
        )
        return _empty_data_handler(request)

    result = _backfill_admin_for_tenant(
        tenant,
        days=21,
        chunk_days=7,
        client=_stub_client(tracking_handler),
    )

    assert result["chunks_processed"] == 3
    assert len(seen_windows) == 3

    # Each window is exactly 7 days.
    for start_str, end_str in seen_windows:
        start = datetime.fromisoformat(start_str)
        end = datetime.fromisoformat(end_str)
        assert end - start == timedelta(days=7), (
            f"chunk window not 7 days: {start} → {end}"
        )

    # Windows are contiguous — each chunk's end matches the next chunk's start.
    for i in range(len(seen_windows) - 1):
        prev_end = datetime.fromisoformat(seen_windows[i][1])
        next_start = datetime.fromisoformat(seen_windows[i + 1][0])
        assert prev_end == next_start, (
            f"non-contiguous chunks: {prev_end} → {next_start}"
        )


def test_backfill_resumes_after_crash(clean_backfill_state: None) -> None:
    """A mid-backfill crash leaves the cursor at the last successful chunk;
    a follow-up call picks up from there and completes the remainder.
    """
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_admin import (
        SOURCE_API_ADMIN,
        _backfill_admin_for_tenant,
    )

    tenant = "test-backfill-crash"
    call_count = {"n": 0}

    def crashing_handler(request: httpx.Request) -> httpx.Response:
        # T5.5.6: backfill calls /v1/organizations/workspaces once at
        # start. Don't count that against the crash trigger — the
        # test pins behavior at chunk granularity, not HTTP-call
        # granularity.
        if is_workspaces_request(request):
            return empty_workspaces_response()
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise httpx.NetworkError("simulated mid-backfill crash")
        return _empty_data_handler(request)

    # First attempt: crashes on the 3rd HTTP call (= chunk 3 of 3).
    with pytest.raises(httpx.NetworkError):
        _backfill_admin_for_tenant(
            tenant,
            days=21,
            chunk_days=7,
            client=_stub_client(crashing_handler),
        )

    # Cursor advanced through 2 successful chunks. The third (failed)
    # chunk never wrote its cursor.
    with session_scope(tenant) as s:
        row = s.execute(
            sql_text(
                "SELECT cursor FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :sa"
            ),
            {"t": tenant, "sa": SOURCE_API_ADMIN},
        ).first()
    assert row is not None
    cursor_after_crash = datetime.fromisoformat(row.cursor)

    # End of chunk 2 is ~7 days before now (since chunk 3 of 3 was the
    # failing one). Allow 1-minute slack for test timing.
    now = datetime.now(timezone.utc)
    delta_to_now = now - cursor_after_crash
    assert timedelta(days=7) - timedelta(minutes=1) <= delta_to_now, (
        f"cursor too far back: {delta_to_now}"
    )
    assert delta_to_now <= timedelta(days=7) + timedelta(minutes=1), (
        f"cursor not advanced enough: {delta_to_now}"
    )

    # Second attempt: handler no longer crashes. Picks up from cursor
    # and walks the remaining window. The captured "now" advances
    # between runs (test wall-clock time), so the second call sees
    # one full 7-day chunk for the originally-failed window PLUS
    # potentially a tiny extra chunk for the inter-run gap.
    seen_windows: list[tuple[str, str]] = []

    def tracking_handler(request: httpx.Request) -> httpx.Response:
        # T5.5.6: backfill also hits /v1/organizations/workspaces.
        # Don't count those against the 7-day-window tracker —
        # `seen_windows` pins the usage-endpoint windows only.
        if is_workspaces_request(request):
            return empty_workspaces_response()
        seen_windows.append(
            (
                request.url.params["starting_at"],
                request.url.params["ending_at"],
            )
        )
        return _empty_data_handler(request)

    result = _backfill_admin_for_tenant(
        tenant,
        days=21,
        chunk_days=7,
        client=_stub_client(tracking_handler),
    )

    # The first chunk on resume MUST start at the post-crash cursor —
    # this is the actual "resume works" property. Not at backfill_start
    # (that would mean a full re-run; the test would catch it as 3
    # chunks instead of 1 or 2).
    assert seen_windows, "resume made no HTTP call"
    first_chunk_start = datetime.fromisoformat(seen_windows[0][0])
    assert first_chunk_start == cursor_after_crash, (
        f"resume did not pick up from cursor: "
        f"first chunk starts at {first_chunk_start}, "
        f"expected {cursor_after_crash}"
    )

    # 1 or 2 chunks: the failed 7-day window, plus possibly a tiny
    # tail covering the wall-clock elapsed between the two runs.
    assert result["chunks_processed"] in (1, 2), (
        f"expected 1 or 2 chunks on resume, got {result['chunks_processed']}"
    )

    # Cursor now sits at-or-near now.
    with session_scope(tenant) as s:
        row = s.execute(
            sql_text(
                "SELECT cursor FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :sa"
            ),
            {"t": tenant, "sa": SOURCE_API_ADMIN},
        ).first()
    cursor_final = datetime.fromisoformat(row.cursor)
    assert datetime.now(timezone.utc) - cursor_final <= timedelta(
        minutes=1
    ), f"final cursor not near now: {cursor_final}"
