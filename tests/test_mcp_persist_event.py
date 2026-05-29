# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM1 — Celery persist_event task tests.

Hits the real chain primitive (``vargate_telemetry.chain.append_telemetry_record``)
because the chain's tamper-detect semantics are part of the persist
task's contract — mocking would let a bug in the chain integration
hide here.

The Celery task is invoked synchronously by calling its function
body (``persist_event.run`` semantics via the underlying function),
not via ``.delay()``. That skips the broker entirely, which is
fine for testing the persistence behavior. The ``.delay`` happy
path is covered by ``test_mcp_log_interaction.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import text as sql_text


@pytest.fixture
def clean_records() -> Iterator[None]:
    """Empty telemetry_records before and after each test."""
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


def _persist(
    *,
    event_id: str = "",
    tenant_id: str = "tnt_us_persist_test",
    user_id: str = "user-persist-test",
    user_email: str = "persist-test@example.com",
    kind: str = "chat",
    model: str = "claude-opus-4-7",
    summary: str = "Built the test fixtures.",
    input_tokens_estimate: int = 1200,
    output_tokens_estimate: int = 400,
    tool_calls_count: int = 1,
    surface: str | None = None,
    client_received_at: str = "",
) -> dict:
    """Invoke the task body directly (no broker).

    Celery's ``@task(bind=True)`` wires ``self`` from the task
    instance; we re-bind the function to the task at call-time so
    the ``self.retry(...)`` branch is still reachable from tests
    that want to exercise it.
    """
    from mcp_server.tasks.persist_event import persist_event

    if not event_id:
        event_id = str(uuid4())
    if not client_received_at:
        client_received_at = datetime.now(timezone.utc).isoformat()

    return persist_event.run(
        event_id=event_id,
        tenant_id=tenant_id,
        user_id=user_id,
        user_email=user_email,
        kind=kind,
        model=model,
        summary=summary,
        input_tokens_estimate=input_tokens_estimate,
        output_tokens_estimate=output_tokens_estimate,
        tool_calls_count=tool_calls_count,
        surface=surface,
        client_received_at=client_received_at,
    )


# ───────────────────────────────────────────────────────────────────────────
# Happy path: row + chain entry
# ───────────────────────────────────────────────────────────────────────────


def test_persist_event_writes_mcp_row(clean_records: None) -> None:
    """One call → one row in telemetry_records with source_api='mcp'.

    Round-trip check: the row's external_id matches the documented
    ``mcp:{tenant_id}:{user_id}:{event_id}`` format, source_api is
    the new 'mcp' string value, record_type is 'mcp_interaction'.
    """
    from vargate_telemetry.db import engine

    event_id = str(uuid4())
    result = _persist(event_id=event_id)
    assert result["persisted"] is True
    assert result["event_id"] == event_id

    with engine.connect() as conn:
        row = conn.execute(
            sql_text(
                """
                SELECT tenant_id, source_api, record_type, external_id,
                       subject_user_id, content_hash, chain_seq
                FROM telemetry_records
                WHERE tenant_id = :t AND source_api = 'mcp'
                """
            ),
            {"t": "tnt_us_persist_test"},
        ).first()
    assert row is not None
    assert row.source_api == "mcp"
    assert row.record_type == "mcp_interaction"
    assert row.external_id == (
        f"mcp:tnt_us_persist_test:user-persist-test:{event_id}"
    )
    assert row.subject_user_id == "user-persist-test"
    assert len(row.content_hash) == 32  # SHA-256
    assert row.chain_seq >= 1


def test_persist_event_dedupes_on_replay(clean_records: None) -> None:
    """Re-delivery of the same event_id is an idempotent no-op.

    The Celery broker may at-least-once redeliver a task. The
    ``UNIQUE (tenant_id, source_api, external_id)`` constraint on
    telemetry_records turns that into IntegrityError, which the task
    catches + returns a ``persisted=False, reason=dedup`` result.
    """
    from vargate_telemetry.db import engine

    event_id = str(uuid4())
    first = _persist(event_id=event_id)
    second = _persist(event_id=event_id)

    assert first["persisted"] is True
    assert second["persisted"] is False
    assert second["reason"] == "dedup"

    with engine.connect() as conn:
        count = conn.execute(
            sql_text(
                "SELECT COUNT(*) FROM telemetry_records "
                "WHERE tenant_id = :t AND source_api = 'mcp'"
            ),
            {"t": "tnt_us_persist_test"},
        ).scalar()
    assert count == 1


def test_persist_event_chain_advances_seq(clean_records: None) -> None:
    """Two distinct events → two chain_seq slots, second's prev_hash links to first.

    This isn't a full chain-verify test (those live in
    ``test_telemetry_chain.py``) — it's a smoke check that the
    persist task is wired through the chain primitive correctly.
    """
    from vargate_telemetry.db import engine

    first_id = str(uuid4())
    second_id = str(uuid4())
    _persist(event_id=first_id)
    _persist(event_id=second_id)

    with engine.connect() as conn:
        rows = conn.execute(
            sql_text(
                """
                SELECT chain_seq, chain_prev_hash, chain_self_hash
                FROM telemetry_records
                WHERE tenant_id = :t AND source_api = 'mcp'
                ORDER BY chain_seq
                """
            ),
            {"t": "tnt_us_persist_test"},
        ).fetchall()
    assert len(rows) == 2
    assert rows[1].chain_seq == rows[0].chain_seq + 1
    assert rows[1].chain_prev_hash == rows[0].chain_self_hash


def test_persist_event_requires_tenant_id(clean_records: None) -> None:
    """An empty tenant_id is a coding bug — fail loud, don't write."""
    from vargate_telemetry.db import engine

    with pytest.raises(ValueError, match="tenant_id required"):
        _persist(tenant_id="")

    with engine.connect() as conn:
        count = conn.execute(
            sql_text(
                "SELECT COUNT(*) FROM telemetry_records "
                "WHERE source_api = 'mcp'"
            ),
        ).scalar()
    assert count == 0


# ───────────────────────────────────────────────────────────────────────────
# TM4 #3 — self-reported surface persists into metadata
# ───────────────────────────────────────────────────────────────────────────


def test_persist_event_stores_surface_in_metadata(
    clean_records: None,
) -> None:
    """A reported surface lands verbatim in the record's metadata so the
    read-path can render "Claude Code" vs "Claude (chat)"."""
    from vargate_telemetry.db import engine

    _persist(surface="claude_code")

    with engine.connect() as conn:
        metadata = conn.execute(
            sql_text(
                "SELECT metadata FROM telemetry_records "
                "WHERE tenant_id = :t AND source_api = 'mcp'"
            ),
            {"t": "tnt_us_persist_test"},
        ).scalar()
    assert metadata["surface"] == "claude_code"


def test_persist_event_surface_null_when_not_reported(
    clean_records: None,
) -> None:
    """Omitting surface stores an explicit null (key always present) so a
    pre-field client is indistinguishable from one that left it blank —
    both defer to the read-path's kind heuristic."""
    from vargate_telemetry.db import engine

    _persist()  # no surface

    with engine.connect() as conn:
        metadata = conn.execute(
            sql_text(
                "SELECT metadata FROM telemetry_records "
                "WHERE tenant_id = :t AND source_api = 'mcp'"
            ),
            {"t": "tnt_us_persist_test"},
        ).scalar()
    assert "surface" in metadata
    assert metadata["surface"] is None
