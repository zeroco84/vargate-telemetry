# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Topic-classification Celery task (TM4 Track D / D2).

Beat fan-out + per-tenant classifier, mirroring the ``evaluate_budgets``
dispatcher pattern. Every 15 minutes the dispatcher enumerates active
tenants in the current region; each per-tenant task finds MCP records
that have a summary but no ``interaction_topics`` row, classifies them
in batches via Claude Haiku, and writes the labels.

Runs both **forward** (new MCP records) and **backfills** existing ones,
bounded by ``CLASSIFY_RUN_LIMIT`` per tenant per tick so a large backfill
spreads across ticks rather than doing thousands in one task.

Never fake a label
==================
- A summary the model leaves unclassified (``None``) gets no row — it's
  reprocessed next run.
- A batch whose API call fails (``ClassificationError``) is skipped; its
  records stay unclassified and other batches still run.
- No key (``ClassifierNotConfigured``) aborts the tenant's run cleanly —
  nothing to do until the key is wired.

Region note: like every dispatcher, this defaults to
``VARGATE_REGION=us`` — see the ``ogma_dispatch_region_gap`` finding. For
the eu demo tenant, trigger ``classify_topics_for_tenant.delay(tid)``
directly (as with budget eval).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from sqlalchemy import text as sql_text

from vargate_telemetry.celery_app import celery_app
from vargate_telemetry.db import scheduler_session_scope, session_scope
from vargate_telemetry.topics import TAXONOMY_VERSION
from vargate_telemetry.topics.classifier import (
    BATCH_SIZE,
    CLASSIFIER_MODEL,
    ClassificationError,
    ClassifierNotConfigured,
    classify_summaries,
)

_log = logging.getLogger(__name__)

# Max records classified per tenant per tick. Bounds cost + runtime of any
# single task; a larger backfill drains over successive 15-minute ticks.
CLASSIFY_RUN_LIMIT = 200


@celery_app.task(
    name="vargate_telemetry.tasks.classify_topics.classify_topics_for_tenant",
)
def classify_topics_for_tenant(tenant_id: str) -> dict:
    """Classify this tenant's unclassified MCP summaries (up to the cap).

    Returns a structured dict for the beat log: how many candidates were
    found, how many got a label, how many stayed unclassified.
    """
    classified = 0
    unclassified = 0
    candidates = 0

    with session_scope(tenant_id) as s:
        rows = s.execute(
            sql_text(
                """
                SELECT tr.id::text AS record_id,
                       tr.metadata->>'summary' AS summary
                FROM telemetry_records tr
                WHERE tr.tenant_id = current_setting('app.tenant_id')
                  AND tr.source_api = 'mcp'
                  AND COALESCE(tr.metadata->>'summary', '') <> ''
                  AND NOT EXISTS (
                      SELECT 1 FROM interaction_topics it
                      WHERE it.record_id = tr.id
                        AND it.tenant_id = current_setting('app.tenant_id')
                  )
                ORDER BY tr.occurred_at DESC
                LIMIT :limit
                """
            ),
            {"limit": CLASSIFY_RUN_LIMIT},
        ).all()
        candidates = len(rows)

        for start in range(0, len(rows), BATCH_SIZE):
            batch = rows[start : start + BATCH_SIZE]
            summaries = [r.summary for r in batch]
            try:
                labels: list[Optional[str]] = classify_summaries(summaries)
            except ClassifierNotConfigured:
                # No key — nothing this task can do. Stop cleanly; the
                # records remain unclassified for a future run.
                _log.warning(
                    "classify_topics_for_tenant(%s): ANTHROPIC_API_KEY "
                    "not set; %d records left unclassified",
                    tenant_id,
                    candidates,
                )
                return {
                    "tenant_id": tenant_id,
                    "candidates": candidates,
                    "classified": classified,
                    "unclassified": candidates - classified,
                    "skipped_no_key": True,
                }
            except ClassificationError:
                # Transient/parse failure for this batch — leave its
                # records unclassified, keep going with the next batch.
                _log.exception(
                    "classify_topics_for_tenant(%s): batch failed; "
                    "leaving %d records unclassified, continuing",
                    tenant_id,
                    len(batch),
                )
                unclassified += len(batch)
                continue

            for row, label in zip(batch, labels):
                if label is None:
                    unclassified += 1
                    continue
                s.execute(
                    sql_text(
                        """
                        INSERT INTO interaction_topics (
                            tenant_id, record_id, topic,
                            taxonomy_version, model
                        )
                        VALUES (
                            current_setting('app.tenant_id'),
                            :record_id, :topic, :version, :model
                        )
                        ON CONFLICT (tenant_id, record_id) DO NOTHING
                        """
                    ),
                    {
                        "record_id": row.record_id,
                        "topic": label,
                        "version": TAXONOMY_VERSION,
                        "model": CLASSIFIER_MODEL,
                    },
                )
                classified += 1

    _log.info(
        "classify_topics_for_tenant(%s): %d candidates, %d classified, "
        "%d unclassified",
        tenant_id,
        candidates,
        classified,
        unclassified,
    )
    return {
        "tenant_id": tenant_id,
        "candidates": candidates,
        "classified": classified,
        "unclassified": unclassified,
    }


@celery_app.task(
    name="vargate_telemetry.tasks.classify_topics.dispatch_classify_topics",
)
def dispatch_classify_topics(region: Optional[str] = None) -> int:
    """Beat fan-out. Queue one classify task per active tenant in region.

    Mirrors ``dispatch_evaluate_budgets`` — same role + query shape, same
    VARGATE_REGION default caveat.
    """
    target_region = region or os.environ.get("VARGATE_REGION", "us")

    with scheduler_session_scope() as s:
        rows = s.execute(
            sql_text(
                "SELECT tenant_id FROM tenants "
                "WHERE active = true AND region = :r"
            ),
            {"r": target_region},
        ).all()

    for row in rows:
        classify_topics_for_tenant.delay(row.tenant_id)

    _log.info(
        "dispatch_classify_topics: queued %d tenants in region %s",
        len(rows),
        target_region,
    )
    return len(rows)
