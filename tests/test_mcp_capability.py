# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""TM1 — mcp_connector capability detection.

A tenant gets ``mcp_connector: true`` iff at least one row exists
in ``telemetry_records`` with ``source_api = 'mcp'`` ingested in
the last 90 days. The pre-select-region caller (no tenant_id yet)
always gets False.

These tests exercise ``_tenant_has_recent_mcp_traffic`` directly —
the validate-key end-to-end shape is covered in test_onboarding.py
and test_pull_compliance.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import text as sql_text


@pytest.fixture
def clean_records() -> Iterator[None]:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE"
            )
        )
    yield
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE"
            )
        )


def _persist_mcp_row(
    tenant_id: str = "tnt_us_capability_test",
    days_old: int = 0,
) -> None:
    """Persist one mcp row at a tunable age via the real Celery task body."""
    from mcp_server.tasks.persist_event import persist_event
    from datetime import timedelta

    occurred = datetime.now(timezone.utc) - timedelta(days=days_old)
    persist_event.run(
        event_id=str(uuid4()),
        tenant_id=tenant_id,
        user_id="user-cap-test",
        user_email="cap-test@example.com",
        kind="chat",
        model="claude-opus-4-7",
        summary="cap test row",
        input_tokens_estimate=100,
        output_tokens_estimate=50,
        tool_calls_count=1,
        client_received_at=occurred.isoformat(),
    )


def test_no_tenant_id_is_false(clean_records: None) -> None:
    """Pre-tenant onboarding callers always get False — they can't
    have any MCP traffic yet by construction."""
    from vargate_telemetry.api.onboarding import (
        _tenant_has_recent_mcp_traffic,
    )

    assert _tenant_has_recent_mcp_traffic(None) is False
    assert _tenant_has_recent_mcp_traffic("") is False


def test_tenant_with_no_mcp_rows_is_false(clean_records: None) -> None:
    """A tenant whose Anthropic ingest is healthy but who has never
    used the MCP server returns False."""
    from vargate_telemetry.api.onboarding import (
        _tenant_has_recent_mcp_traffic,
    )

    assert _tenant_has_recent_mcp_traffic("tnt_us_capability_test") is False


def test_tenant_with_recent_mcp_row_is_true(clean_records: None) -> None:
    """One row in the last 90 days flips the bit to True."""
    from vargate_telemetry.api.onboarding import (
        _tenant_has_recent_mcp_traffic,
    )

    _persist_mcp_row(days_old=0)
    assert _tenant_has_recent_mcp_traffic("tnt_us_capability_test") is True
