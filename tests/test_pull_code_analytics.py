# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the T5.4 Code Analytics ingestion pipeline.

Mirrors ``test_pull_compliance.py`` structure with adjustments for the
endpoint's daily-aggregation + page-token semantics:

  - ``test_pull_code_analytics_writes_records_to_postgres`` — happy
    path, two records on one day.
  - ``test_pull_code_analytics_skips_when_403`` — InsufficientScope
    surfaces as a soft-skip dict (rare per the docs but documented
    in the API ref as the AWS-platform exception).
  - ``test_pull_code_analytics_cursor_advances_on_dedup_only_run`` —
    second pull of the same day dedups; cursor still advances
    (matches T5.3's documented invariant for the Activity Feed
    pull).
  - ``test_pull_code_analytics_honors_max_pages_cap`` — large response
    triggers the pages-per-invocation cap and yields cleanly to the
    next tick.
  - ``test_pull_code_analytics_handles_unknown_nested_fields`` — a
    record with novel/unknown nested keys (e.g. a future
    ``review_tool``, a never-seen ``customer_type``) parses without
    raising, lands in record_metadata verbatim.

All tests use ``httpx.MockTransport`` for deterministic response
sequences — same pattern as ``test_pull_compliance.py`` and
``test_pull_admin.py``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Callable, Iterator

import httpx
import pytest
from sqlalchemy import text as sql_text

from vargate_telemetry.anthropic import AnthropicAdminClient


# ───────────────────────────────────────────────────────────────────────────
# Sample Code Analytics payloads (lifted from the public docs)
# ───────────────────────────────────────────────────────────────────────────


_FIXED_DAY = date(2026, 5, 11)
_FIXED_DAY_ISO = _FIXED_DAY.isoformat()


def _record(actor_email: str, day_iso: str = _FIXED_DAY_ISO) -> dict:
    """One Code Analytics record for a user actor on the given day.

    ``day_iso`` defaults to ``_FIXED_DAY_ISO`` for single-day tests.
    Multi-day tests pass the day explicitly so external_id (which
    keys off date + actor) gets a unique value per day and dedup
    doesn't fire across days.
    """
    return {
        "date": f"{day_iso}T00:00:00Z",
        "actor": {
            "type": "user_actor",
            "email_address": actor_email,
        },
        "organization_id": "dc9f6c26-b22c-4831-8d01-0446bada88f1",
        "customer_type": "api",
        "terminal_type": "vscode",
        "core_metrics": {
            "num_sessions": 5,
            "lines_of_code": {"added": 1543, "removed": 892},
            "commits_by_claude_code": 12,
            "pull_requests_by_claude_code": 2,
        },
        "tool_actions": {
            "edit_tool": {"accepted": 45, "rejected": 5},
            "multi_edit_tool": {"accepted": 12, "rejected": 2},
            "write_tool": {"accepted": 8, "rejected": 1},
            "notebook_edit_tool": {"accepted": 3, "rejected": 0},
        },
        "model_breakdown": [
            {
                "model": "claude-opus-4-7",
                "tokens": {
                    "input": 100000,
                    "output": 35000,
                    "cache_read": 10000,
                    "cache_creation": 5000,
                },
                "estimated_cost": {"currency": "USD", "amount": 1025},
            }
        ],
    }


def _two_record_handler(request: httpx.Request) -> httpx.Response:
    """One-page response with two distinct user records."""
    return httpx.Response(
        200,
        json={
            "data": [_record("alice@example.com"), _record("bob@example.com")],
            "has_more": False,
            "next_page": None,
        },
    )


def _stub_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AnthropicAdminClient:
    return AnthropicAdminClient(
        api_key="test-key",
        base_url="https://api.test",
        min_wait=0.0,
        max_wait=0.0,
        wait_multiplier=0.0,
        transport=httpx.MockTransport(handler),
    )


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_pull_state() -> Iterator[None]:
    """Empty every ingest-touched table + Redis meter keys. Mirrors the
    pull_compliance fixture."""
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


# ───────────────────────────────────────────────────────────────────────────
# Happy path — two records land in telemetry_records with correct shape
# ───────────────────────────────────────────────────────────────────────────


def test_pull_code_analytics_writes_records_to_postgres(
    clean_pull_state: None,
) -> None:
    """Two records on one day → two telemetry_records rows with
    record_type='code_analytics', source_api='code_analytics', and the
    full record JSON in record_metadata (including nested
    core_metrics / tool_actions / model_breakdown)."""
    from vargate_telemetry.chain import verify_telemetry_chain
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_code_analytics import (
        SOURCE_API_CODE_ANALYTICS,
        _pull_code_analytics_for_tenant,
    )

    tenant = "test-code-write"

    # `since=_FIXED_DAY` and `today=_FIXED_DAY + 1` pins the window to
    # exactly one day: yesterday is _FIXED_DAY, today is the day
    # after (so today's data is excluded by the INGEST_LAG_DAYS rule).
    result = _pull_code_analytics_for_tenant(
        tenant,
        since=_FIXED_DAY,
        today=date(2026, 5, 12),
        client=_stub_client(_two_record_handler),
    )

    assert result["status"] == "ok"
    assert result["records_pulled"] == 2
    assert result["records_deduped"] == 0
    assert result["days_processed"] == 1

    # Read back the rows. Match T5.3's SQL convention — column is
    # `metadata`, Python attribute is `record_metadata`.
    with session_scope(tenant) as s:
        rows = s.execute(
            sql_text(
                "SELECT external_id, record_type, source_api, "
                "       metadata::text AS meta_json "
                "FROM telemetry_records "
                "WHERE tenant_id = :t "
                "ORDER BY chain_seq"
            ),
            {"t": tenant},
        ).all()

    assert len(rows) == 2
    assert {r.external_id for r in rows} == {
        "code_analytics:2026-05-11:alice@example.com",
        "code_analytics:2026-05-11:bob@example.com",
    }
    for r in rows:
        assert r.record_type == "code_analytics"
        assert r.source_api == SOURCE_API_CODE_ANALYTICS
        meta = json.loads(r.meta_json)
        # Nested objects survived via extra="allow" — the full record
        # JSON landed in record_metadata.
        assert meta["core_metrics"]["num_sessions"] == 5
        assert meta["tool_actions"]["edit_tool"]["accepted"] == 45
        assert (
            meta["model_breakdown"][0]["model"] == "claude-opus-4-7"
        )

    # Chain holds — content_hash is bound into canonical bytes, both
    # records linked into the per-tenant chain.
    chain_result = verify_telemetry_chain(tenant)
    assert chain_result.valid is True
    assert chain_result.record_count == 2


# ───────────────────────────────────────────────────────────────────────────
# 403 soft-skip — InsufficientScope from the endpoint
# ───────────────────────────────────────────────────────────────────────────


def test_pull_code_analytics_skips_when_403(
    clean_pull_state: None,
) -> None:
    """The endpoint is "free to use for all Admin-API-capable orgs"
    per Anthropic's docs — but the docs also call out exceptions
    (e.g., Claude Platform on AWS). When the endpoint 403s, the
    pure-Python helper catches InsufficientScope and returns a
    soft-skip dict (`status='no_code_analytics_access'`) rather than
    propagating the exception. The Celery wrapper would NOT retry
    on this — the dispatcher sees a successful return and the tenant
    gets skipped this tick.
    """
    from vargate_telemetry.tasks.pull_code_analytics import (
        _pull_code_analytics_for_tenant,
    )

    def handler_403(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "type": "permission_error",
                    "message": "claude_code analytics not available",
                }
            },
        )

    tenant = "test-code-no-access"
    result = _pull_code_analytics_for_tenant(
        tenant,
        since=_FIXED_DAY,
        today=date(2026, 5, 12),
        client=_stub_client(handler_403),
    )

    assert result["status"] == "no_code_analytics_access"
    assert result["records_pulled"] == 0
    assert result["records_deduped"] == 0
    assert result["days_processed"] == 0


# ───────────────────────────────────────────────────────────────────────────
# Dedup-only cursor advance — second pull dedups all rows; cursor still advances
# ───────────────────────────────────────────────────────────────────────────


def test_pull_code_analytics_cursor_advances_on_dedup_only_run(
    clean_pull_state: None,
) -> None:
    """First pull inserts two rows + advances cursor to fixed day.
    Second pull on the same day dedups both rows but ALSO advances the
    cursor (matches T5.3's documented invariant — a dedup'd day is a
    fully-ingested day, no point re-querying it forever).

    The cursor advance on dedup-only is the load-bearing property
    here: without it, a steady-state tenant whose data hasn't changed
    today would have its cursor stuck at the same day on every tick.
    """
    from vargate_telemetry.db import engine
    from vargate_telemetry.tasks.pull_code_analytics import (
        SOURCE_API_CODE_ANALYTICS,
        _pull_code_analytics_for_tenant,
    )

    tenant = "test-code-dedup-cursor"

    # First pull: 2 inserted, cursor advances to _FIXED_DAY midnight.
    first = _pull_code_analytics_for_tenant(
        tenant,
        since=_FIXED_DAY,
        today=date(2026, 5, 12),
        client=_stub_client(_two_record_handler),
    )
    assert first["records_pulled"] == 2
    assert first["records_deduped"] == 0
    assert first["days_processed"] == 1

    # Read the cursor after the first pull.
    with engine.begin() as conn:
        row1 = conn.execute(
            sql_text(
                "SELECT cursor FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :s"
            ),
            {"t": tenant, "s": SOURCE_API_CODE_ANALYTICS},
        ).first()
    cursor_after_first = datetime.fromisoformat(row1.cursor)
    assert cursor_after_first.date() == _FIXED_DAY

    # Second pull of the same day: all 2 dedup, days_processed still
    # 1, cursor STAYS at _FIXED_DAY (it can't go backward, and the
    # window only covered _FIXED_DAY itself).
    second = _pull_code_analytics_for_tenant(
        tenant,
        since=_FIXED_DAY,
        today=date(2026, 5, 12),
        client=_stub_client(_two_record_handler),
    )
    assert second["records_pulled"] == 0
    assert second["records_deduped"] == 2
    assert second["days_processed"] == 1, (
        "Cursor must advance even when every row dedup'd — the day is "
        "fully ingested and shouldn't block forever."
    )

    # Cursor unchanged (already at the right day).
    with engine.begin() as conn:
        row2 = conn.execute(
            sql_text(
                "SELECT cursor FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :s"
            ),
            {"t": tenant, "s": SOURCE_API_CODE_ANALYTICS},
        ).first()
    assert datetime.fromisoformat(row2.cursor).date() == _FIXED_DAY


# ───────────────────────────────────────────────────────────────────────────
# MAX_PAGES_PER_INVOCATION honored — bounded per-tick work
# ───────────────────────────────────────────────────────────────────────────


def test_pull_code_analytics_honors_max_pages_cap(
    clean_pull_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A very-large window (many days) hits the per-invocation page
    cap and yields cleanly to the next 15-minute tick. The cursor
    advances through the days that were processed; the rest resumes
    on the next call.

    We monkey-patch ``MAX_PAGES_PER_INVOCATION`` to a small value (3)
    for the test so we don't have to construct hundreds of pages.
    """
    from vargate_telemetry.tasks import pull_code_analytics

    monkeypatch.setattr(pull_code_analytics, "MAX_PAGES_PER_INVOCATION", 3)
    monkeypatch.setattr(
        pull_code_analytics, "DEFAULT_PER_PAGE_LIMIT", 2
    )

    # 5-day window: 2026-05-07 through 2026-05-11 inclusive. With page
    # limit 2 + 2 records per day (one page per day), 3 pages = 3 days
    # processed before the cap fires. Cursor lands at the 3rd day,
    # remaining 2 days resume next tick.
    #
    # Per-day-aware handler: reads `starting_at` from the request URL
    # and returns records dated for that day. Without this, MockTransport
    # would return the same fixed-date records for every day and dedup
    # would absorb days 2/3, masking the records_pulled count.
    def _per_day_handler(request: httpx.Request) -> httpx.Response:
        day = request.url.params.get("starting_at", _FIXED_DAY_ISO)
        return httpx.Response(
            200,
            json={
                "data": [
                    _record("alice@example.com", day),
                    _record("bob@example.com", day),
                ],
                "has_more": False,
                "next_page": None,
            },
        )

    tenant = "test-code-page-cap"
    result = pull_code_analytics._pull_code_analytics_for_tenant(
        tenant,
        since=date(2026, 5, 7),
        today=date(2026, 5, 12),  # yesterday = 2026-05-11
        client=_stub_client(_per_day_handler),
        per_page_limit=2,
    )

    # Cap fires after 3 days (each day = one page of 2 records).
    assert result["status"] == "ok"
    assert result["days_processed"] == 3
    assert result["records_pulled"] == 6  # 2/day × 3 days

    # Cursor should land at the 3rd day (2026-05-09), so the next
    # invocation starts at 2026-05-10.
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        row = conn.execute(
            sql_text(
                "SELECT cursor FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :s"
            ),
            {
                "t": tenant,
                "s": pull_code_analytics.SOURCE_API_CODE_ANALYTICS,
            },
        ).first()
    assert datetime.fromisoformat(row.cursor).date() == date(2026, 5, 9)


# ───────────────────────────────────────────────────────────────────────────
# Forward-compat: unknown nested fields parse cleanly
# ───────────────────────────────────────────────────────────────────────────


def test_pull_code_analytics_handles_unknown_nested_fields(
    clean_pull_state: None,
) -> None:
    """A record with a previously-unseen tool (``super_review_tool``),
    a novel ``customer_type``, and an extra unexpected top-level field
    must parse and land in record_metadata without raising.

    This is the load-bearing property of the
    flat-model-with-extra-allow pattern: new Anthropic-side fields
    absorb cleanly rather than crashing the ingest pipeline.
    """
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_code_analytics import (
        _pull_code_analytics_for_tenant,
    )

    def handler_unknown(request: httpx.Request) -> httpx.Response:
        rec = _record("future@example.com")
        # Inject novel tool key inside tool_actions.
        rec["tool_actions"]["super_review_tool"] = {
            "accepted": 7,
            "rejected": 1,
        }
        # Novel customer type.
        rec["customer_type"] = "marketplace_b2b"
        # Top-level extra that the model doesn't know about.
        rec["seasonal_promotion_credits"] = 42
        return httpx.Response(
            200,
            json={"data": [rec], "has_more": False, "next_page": None},
        )

    tenant = "test-code-unknown-fields"
    result = _pull_code_analytics_for_tenant(
        tenant,
        since=_FIXED_DAY,
        today=date(2026, 5, 12),
        client=_stub_client(handler_unknown),
    )

    assert result["status"] == "ok"
    assert result["records_pulled"] == 1

    with session_scope(tenant) as s:
        row = s.execute(
            sql_text(
                "SELECT metadata::text AS meta_json "
                "FROM telemetry_records WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).first()

    meta = json.loads(row.meta_json)
    # Novel tool present in stored metadata.
    assert meta["tool_actions"]["super_review_tool"]["accepted"] == 7
    # Novel customer_type round-tripped (it's a documented field on
    # the model — the value, not the field, is what's novel).
    assert meta["customer_type"] == "marketplace_b2b"
    # Top-level extra survived via extra="allow".
    assert meta["seasonal_promotion_credits"] == 42
