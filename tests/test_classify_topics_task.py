# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the classify_topics Celery task (TM4 Track D / D2).

Mocks ``classify_summaries`` (the LLM seam) so these exercise the task's
DB behavior — candidate selection, dedup, never-fake-a-label, batch
failure isolation — without any network or anthropic SDK.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Iterator, Optional

import pytest
from sqlalchemy import text as sql_text

from vargate_telemetry.topics import classifier
from vargate_telemetry.topics.classifier import (
    ClassificationError,
    ClassifierNotConfigured,
)
from vargate_telemetry.tasks import classify_topics


@pytest.fixture
def clean() -> Iterator[None]:
    from vargate_telemetry.db import engine

    def _truncate() -> None:
        with engine.begin() as conn:
            conn.execute(
                sql_text(
                    "TRUNCATE TABLE interaction_topics RESTART IDENTITY CASCADE"
                )
            )
            conn.execute(
                sql_text(
                    "TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE"
                )
            )

    _truncate()
    yield
    _truncate()


def _provision_tenant(tenant_id: str) -> None:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO tenants (tenant_id, region, active, billing_status)
                VALUES (:t, 'us', TRUE, 'trial')
                ON CONFLICT (tenant_id) DO NOTHING
                """
            ),
            {"t": tenant_id},
        )


def _seed_mcp(tenant_id: str, summary: str) -> str:
    """Insert one MCP record with the given summary. Returns its id."""
    from vargate_telemetry.db import engine

    rid = str(uuid.uuid4())
    md = {"kind": "chat", "model": "claude-x", "summary": summary}
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    id, tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :id, :t, 'mcp_interaction', 'mcp', :eid,
                    :occurred_at, decode(:zero32, 'hex'), :metadata,
                    (SELECT COALESCE(MAX(chain_seq), 0) + 1
                       FROM telemetry_records WHERE tenant_id = :t_lookup),
                    decode(:zero32, 'hex'), decode(:one32, 'hex')
                )
                """
            ),
            {
                "id": rid,
                "t": tenant_id,
                "t_lookup": tenant_id,
                "eid": f"mcp:{tenant_id}:{rid}",
                "occurred_at": datetime.now(tz=timezone.utc),
                "zero32": "00" * 32,
                "one32": "11" * 32,
                "metadata": json.dumps(md),
            },
        )
    return rid


def _preclassify(tenant_id: str, record_id: str, topic: str) -> None:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO interaction_topics
                    (tenant_id, record_id, topic, taxonomy_version, model)
                VALUES (:t, :rid, :topic, 'v1', 'seed')
                """
            ),
            {"t": tenant_id, "rid": record_id, "topic": topic},
        )


def _topics(tenant_id: str) -> list:
    from vargate_telemetry.db import engine

    with engine.connect() as conn:
        return conn.execute(
            sql_text(
                "SELECT record_id::text AS record_id, topic, "
                "taxonomy_version, model FROM interaction_topics "
                "WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        ).all()


def _mock_labels(
    monkeypatch: pytest.MonkeyPatch, mapping: dict[str, Optional[str]]
) -> list:
    """Patch classify_summaries to map summary→label; record the calls."""
    calls: list = []

    def fake(summaries: list[str]) -> list:
        calls.append(list(summaries))
        return [mapping.get(s) for s in summaries]

    monkeypatch.setattr(classify_topics, "classify_summaries", fake)
    return calls


# ───────────────────────────────────────────────────────────────────────────


def test_classifies_unlabeled_mcp_records(
    clean: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = "tnt_us_classify_basic"
    _provision_tenant(tenant)
    _seed_mcp(tenant, "fixed a null-pointer bug")
    _seed_mcp(tenant, "drafted the launch blog post")
    _mock_labels(
        monkeypatch,
        {
            "fixed a null-pointer bug": "Coding",
            "drafted the launch blog post": "Writing & content",
        },
    )

    result = classify_topics.classify_topics_for_tenant(tenant)

    assert result["candidates"] == 2
    assert result["classified"] == 2
    rows = _topics(tenant)
    assert {r.topic for r in rows} == {"Coding", "Writing & content"}
    # Rows are stamped with the taxonomy version + classifying model.
    assert all(r.taxonomy_version == "v1" for r in rows)
    assert all(r.model == classifier.CLASSIFIER_MODEL for r in rows)


def test_skips_already_classified_records(
    clean: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = "tnt_us_classify_dedup"
    _provision_tenant(tenant)
    done = _seed_mcp(tenant, "already labeled")
    _preclassify(tenant, done, "Coding")
    _seed_mcp(tenant, "needs a label")
    calls = _mock_labels(monkeypatch, {"needs a label": "Research"})

    result = classify_topics.classify_topics_for_tenant(tenant)

    # Only the unlabeled record is a candidate / sent to the classifier.
    assert result["candidates"] == 1
    assert calls == [["needs a label"]]
    # The pre-existing label is untouched; the new one is added.
    rows = {r.record_id: r.topic for r in _topics(tenant)}
    assert rows[done] == "Coding"
    assert len(rows) == 2


def test_skips_records_without_summary(
    clean: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = "tnt_us_classify_nosummary"
    _provision_tenant(tenant)
    _seed_mcp(tenant, "")  # empty summary — not a candidate
    _mock_labels(monkeypatch, {})

    result = classify_topics.classify_topics_for_tenant(tenant)

    assert result["candidates"] == 0
    assert _topics(tenant) == []


def test_none_label_leaves_record_unclassified(
    clean: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A summary the model didn't classify gets NO row — never guessed."""
    tenant = "tnt_us_classify_none"
    _provision_tenant(tenant)
    _seed_mcp(tenant, "ambiguous one-liner")
    _mock_labels(monkeypatch, {"ambiguous one-liner": None})

    result = classify_topics.classify_topics_for_tenant(tenant)

    assert result["candidates"] == 1
    assert result["classified"] == 0
    assert result["unclassified"] == 1
    assert _topics(tenant) == []


def test_batch_failure_writes_no_rows_and_does_not_raise(
    clean: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = "tnt_us_classify_err"
    _provision_tenant(tenant)
    _seed_mcp(tenant, "a")
    _seed_mcp(tenant, "b")

    def boom(summaries: list[str]) -> list:
        raise ClassificationError("transient")

    monkeypatch.setattr(classify_topics, "classify_summaries", boom)

    # The task must NOT raise — it logs, leaves the batch unclassified.
    result = classify_topics.classify_topics_for_tenant(tenant)
    assert result["classified"] == 0
    assert _topics(tenant) == []


def test_no_key_aborts_cleanly(
    clean: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = "tnt_us_classify_nokey"
    _provision_tenant(tenant)
    _seed_mcp(tenant, "x")

    def not_configured(summaries: list[str]) -> list:
        raise ClassifierNotConfigured("no key")

    monkeypatch.setattr(
        classify_topics, "classify_summaries", not_configured
    )

    result = classify_topics.classify_topics_for_tenant(tenant)
    assert result["skipped_no_key"] is True
    assert _topics(tenant) == []
