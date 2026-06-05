# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""OpenAI Admin costs pull task (TM8 Phase B).

Pulls **authoritative billed spend** from the OpenAI Admin ``/costs``
endpoint at ``project_id`` / ``line_item`` grain (recon §3). This is the
complement to ``pull_openai_usage``: usage gives per-user/per-model
token detail + our own cost *estimate*; costs gives the actual billed
amount (including non-token line items like fine-tune training that a
tokens×pricing estimate can never reproduce). Forecasting / total spend
prefers costs; per-user spend is derived from usage.

Two Celery tasks compose the pipeline (same shape as ``pull_admin``):

  - ``dispatch_openai_costs_pulls`` — beat fan-out over active tenants.
  - ``pull_openai_costs_for_tenant`` — per-tenant. Loads the
    ``(tenant, "openai_admin_costs")`` cursor, iterates ``/costs`` over
    ``[cursor, now)`` with ``group_by=line_item,project_id``, and
    normalizes each ``CostResult`` to a record.

Key differences from usage
===========================

  - **No ``user_id``.** Costs group only by ``project_id`` /
    ``line_item`` — so no ``subject_user_id`` and no cross-vendor
    attribution from this stream.
  - **``bucket_width=1d`` only** (recon §6).
  - **The amount is a ``Decimal``** (``amount.value``, parsed via
    ``Decimal(str(value))`` in the type to survive sci-notation and
    avoid float drift). Stored as a string in ``record_metadata`` so it
    stays exact through JSON.
  - **``/costs`` is slow** (~5 s observed, recon §5) — the production
    client gets a generous timeout via the factory.

``external_id`` (recon-pinned)::

    openai:openai_admin_costs:{start}:{end}:{line_item}:{project_id}

with ``-`` for any missing segment. ``start`` / ``end`` are the bucket
window as integer epoch seconds.

Dedup + cursor + 403 soft-skip are identical to ``pull_openai_usage``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

from vargate_telemetry.celery_app import celery_app
from vargate_telemetry.chain import append_telemetry_record
from vargate_telemetry.db import scheduler_session_scope, session_scope
from vargate_telemetry.metering import increment
from vargate_telemetry.openai import (
    CostBucket,
    CostResult,
    InsufficientScope,
    OpenAIAdminClient,
    admin_client_for_tenant,
)

_log = logging.getLogger(__name__)

# Source-API name used in pull_state + telemetry_records for this stream.
SOURCE_API_OPENAI_COSTS = "openai_admin_costs"

# Recon §3: costs group only by line_item + project_id (no user_id).
COSTS_GROUP_BY = ["line_item", "project_id"]

# How far back to look on first run when no cursor exists.
DEFAULT_INITIAL_LOOKBACK_DAYS = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


# ───────────────────────────────────────────────────────────────────────────
# Cursor I/O — sibling copies of the pull_admin helpers (layout A).
# ───────────────────────────────────────────────────────────────────────────


def _load_cursor(
    session, tenant_id: str, source_api: str
) -> Optional[datetime]:
    row = session.execute(
        sql_text(
            "SELECT cursor FROM pull_state "
            "WHERE tenant_id = :t AND source_api = :s"
        ),
        {"t": tenant_id, "s": source_api},
    ).first()
    if row is None or row.cursor is None:
        return None
    return datetime.fromisoformat(row.cursor)


def _save_cursor(
    session,
    tenant_id: str,
    source_api: str,
    cursor: datetime,
    *,
    status: str = "ok",
    error: Optional[str] = None,
) -> None:
    session.execute(
        sql_text(
            "INSERT INTO pull_state "
            "(tenant_id, source_api, cursor, last_pulled_at, "
            "last_status, last_error) "
            "VALUES (:t, :s, :c, :now, :status, :err) "
            "ON CONFLICT (tenant_id, source_api) "
            "DO UPDATE SET "
            "  cursor = EXCLUDED.cursor, "
            "  last_pulled_at = EXCLUDED.last_pulled_at, "
            "  last_status = EXCLUDED.last_status, "
            "  last_error = EXCLUDED.last_error"
        ),
        {
            "t": tenant_id,
            "s": source_api,
            "c": cursor.isoformat(),
            "now": _now(),
            "status": status,
            "err": error,
        },
    )


def _normalize_costs(bucket: CostBucket) -> Iterator[dict[str, Any]]:
    """Yield one ``telemetry_records`` insert-kwargs dict per cost row.

    One record per ``bucket.results[i]`` (line_item, project) row so
    per-line-item dedup + chain granularity line up.

    ``external_id`` (recon-pinned)::

        openai:openai_admin_costs:{start}:{end}:{line_item}:{project_id}

    with ``-`` for missing segments. ``line_item`` can contain colons
    (``"gpt-4o-2024-08-06, input"``) — harmless since it's the
    second-to-last segment and the format is positional, not split on
    read.

    ``content_hash`` is SHA-256 over the canonical JSON of the per-row
    wrapper. The ``amount.value`` Decimal is preserved as a string in
    metadata (``amount_value``) so billed spend stays exact through
    JSON; ``no subject_user_id`` (costs have no per-user grain).

    Empty-result buckets emit a sentinel record per bucket so the
    cursor advances and we don't re-pull empty windows forever.
    """
    start_epoch = _epoch(bucket.start_time)
    end_epoch = _epoch(bucket.end_time)
    window = {
        "start_time": bucket.start_time.isoformat(),
        "end_time": bucket.end_time.isoformat(),
    }

    if not bucket.results:
        sub_meta = {**window, "result": None}
        canonical = json.dumps(
            sub_meta, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        yield {
            "record_type": "cost",
            "source_api": SOURCE_API_OPENAI_COSTS,
            "external_id": (
                f"openai:{SOURCE_API_OPENAI_COSTS}:"
                f"{start_epoch}:{end_epoch}:-:-"
            ),
            "occurred_at": bucket.start_time,
            "content_hash": hashlib.sha256(canonical).digest(),
            "record_metadata": sub_meta,
        }
        return

    for result in bucket.results:
        yield _normalize_cost_result(
            result,
            bucket=bucket,
            window=window,
            start_epoch=start_epoch,
            end_epoch=end_epoch,
        )


def _normalize_cost_result(
    result: CostResult,
    *,
    bucket: CostBucket,
    window: dict[str, Any],
    start_epoch: int,
    end_epoch: int,
) -> dict[str, Any]:
    """Normalize a single cost result row → insert kwargs."""
    result_dict = result.model_dump(mode="json")

    line_item = result.line_item or "-"
    project_id = result.project_id or "-"

    # amount.value is a Decimal on the model; preserve it as a string in
    # metadata so the billed amount survives JSON serialization without
    # passing through a binary float.
    amount_value = (
        str(result.amount.value) if result.amount is not None else None
    )
    currency = result.amount.currency if result.amount is not None else None

    sub_meta: dict[str, Any] = {
        **window,
        "result": result_dict,
        # Top-level operational fields for the dashboard's cost view.
        "line_item": result.line_item,
        "project_id": result.project_id,
        "project_name": result.project_name,
        "amount_value": amount_value,
        "currency": currency,
    }

    canonical = json.dumps(
        sub_meta, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    return {
        "record_type": "cost",
        "source_api": SOURCE_API_OPENAI_COSTS,
        "external_id": (
            f"openai:{SOURCE_API_OPENAI_COSTS}:"
            f"{start_epoch}:{end_epoch}:{line_item}:{project_id}"
        ),
        "occurred_at": bucket.start_time,
        "content_hash": hashlib.sha256(canonical).digest(),
        "record_metadata": sub_meta,
    }


def _pull_openai_costs_for_tenant(
    tenant_id: str,
    *,
    client: Optional[OpenAIAdminClient] = None,
) -> dict[str, Any]:
    """Pure-Python pull. Returns counts + status.

    Happy path::

        {"records_pulled": N, "records_deduped": M, "status": "ok"}

    403 soft-skip::

        {"records_pulled": 0, "records_deduped": 0,
         "status": "no_openai_costs_access"}
    """
    if not tenant_id:
        raise ValueError("tenant_id required")

    with session_scope(tenant_id) as s:
        cursor = _load_cursor(s, tenant_id, SOURCE_API_OPENAI_COSTS)

    pull_started = _now()
    start_time = cursor or (
        pull_started - timedelta(days=DEFAULT_INITIAL_LOOKBACK_DAYS)
    )

    owned_client = client is None
    if owned_client:
        client = admin_client_for_tenant(tenant_id)

    records_pulled = 0
    records_deduped = 0

    try:
        try:
            for bucket in client.list_costs(
                start_time=start_time,
                end_time=pull_started,
                group_by=COSTS_GROUP_BY,
            ):
                for fields in _normalize_costs(bucket):
                    try:
                        append_telemetry_record(tenant_id, **fields)
                        increment(tenant_id, SOURCE_API_OPENAI_COSTS)
                        records_pulled += 1
                    except IntegrityError:
                        records_deduped += 1
                        _log.info(
                            "pull_openai_costs: dedup hit %s/%s",
                            tenant_id,
                            fields["external_id"],
                        )
        except InsufficientScope:
            _log.info(
                "pull_openai_costs: 403 no_openai_costs_access for %s",
                tenant_id,
            )
            return {
                "records_pulled": 0,
                "records_deduped": 0,
                "status": "no_openai_costs_access",
            }

        with session_scope(tenant_id) as s:
            _save_cursor(
                s,
                tenant_id,
                SOURCE_API_OPENAI_COSTS,
                pull_started,
                status="ok",
            )
    finally:
        if owned_client:
            client.close()

    return {
        "records_pulled": records_pulled,
        "records_deduped": records_deduped,
        "status": "ok",
    }


@celery_app.task(
    bind=True,
    max_retries=3,
    name=(
        "vargate_telemetry.tasks.pull_openai_costs."
        "pull_openai_costs_for_tenant"
    ),
)
def pull_openai_costs_for_tenant(self, tenant_id: str) -> dict[str, Any]:
    """Beat-dispatched per-tenant costs pull. Retries on any exception
    OTHER than the 403 soft-skip (which returns cleanly)."""
    try:
        return _pull_openai_costs_for_tenant(tenant_id)
    except Exception as exc:
        _log.exception("pull_openai_costs failed for %s", tenant_id)
        raise self.retry(exc=exc, countdown=120)


@celery_app.task(
    name=(
        "vargate_telemetry.tasks.pull_openai_costs."
        "dispatch_openai_costs_pulls"
    ),
)
def dispatch_openai_costs_pulls(region: Optional[str] = None) -> int:
    """Beat fan-out. Enumerate active tenants; queue one costs pull each.

    Mirrors ``pull_admin.dispatch_admin_pulls`` — all regions by
    default (TM5 T5.0 region-gap fix), 403 soft-skip in the per-tenant
    task.
    """
    with scheduler_session_scope() as s:
        if region is None:
            rows = s.execute(
                sql_text(
                    "SELECT tenant_id FROM tenants WHERE active = true"
                )
            ).all()
        else:
            rows = s.execute(
                sql_text(
                    "SELECT tenant_id FROM tenants "
                    "WHERE active = true AND region = :r"
                ),
                {"r": region},
            ).all()

    for row in rows:
        pull_openai_costs_for_tenant.delay(row.tenant_id)

    _log.info(
        "dispatch_openai_costs_pulls: queued %d tenants in region %s",
        len(rows),
        region or "all",
    )
    return len(rows)


__all__ = [
    "COSTS_GROUP_BY",
    "DEFAULT_INITIAL_LOOKBACK_DAYS",
    "SOURCE_API_OPENAI_COSTS",
    "_pull_openai_costs_for_tenant",
    "dispatch_openai_costs_pulls",
    "pull_openai_costs_for_tenant",
]
