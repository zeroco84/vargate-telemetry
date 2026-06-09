# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Google Vertex AI token-usage pull task (TM9 Phase A SCAFFOLD).

Vertex's analogue of ``pull_openai_usage`` — pulls per-(model, project,
type) token counts from **Cloud Monitoring** (metric
``aiplatform.googleapis.com/publisher/online_serving/token_count``, label
``type`` ∈ {input, output}) and normalizes each grouped point into a
``telemetry_records`` chain row. This stream carries the token detail +
our own cost *estimate* (via :mod:`vargate_telemetry.pricing.vertex_rates`);
``pull_vertex_costs`` carries the authoritative billed spend from the
BigQuery export.

Two Celery tasks compose the steady-state pipeline (same shape as
``pull_openai_usage`` / ``pull_admin``):

  - ``dispatch_vertex_usage_pulls`` — beat fan-out. Enumerates active
    tenants (all regions by default) and queues one
    ``pull_vertex_usage_for_tenant.delay(tenant_id)`` per row.
  - ``pull_vertex_usage_for_tenant`` — per-tenant. Loads the
    ``(tenant, "vertex_token_usage")`` cursor from ``pull_state`` (a
    per-DAY watermark), reads token_count over ``[cursor_day, today)``,
    normalizes each grouped point to a record, and advances the cursor.

The real work lives in ``_pull_vertex_usage_for_tenant``, a pure-Python
function accepting an optional ``clients`` kwarg — tests inject a fake
``(billing, monitoring, meta)`` tuple; production builds one from the
sealed service-account key via ``vertex.gcp_clients_for_tenant``.

Attribution — project/team ONLY (NO per-user email)
===================================================

Google has **no per-user-email attribution** for Vertex usage. Cloud
Monitoring's token_count is dimensioned by model / location / project —
there is NO end-user identity in the metric. Consequently — and unlike
``pull_openai_usage`` (which sets ``subject_user_id`` + resolves a
``user_email`` for the cross-vendor reconciler) — this stream:

  - sets **no** ``subject_user_id``,
  - writes **no** ``user_email`` and builds **no** email map,
  - is **never** read by the cross-vendor email reconciler,
  - has **no** vertex users side-table.

The attribution dims are ``project.id`` and ``team`` (team labels live
on the *cost* stream's request labels; the monitoring metric itself is
project-grained). The four-dim usage tuple here is (day, model,
project, type).

Token type → cost mapping
=========================

The monitoring metric splits tokens by ``type`` (``input`` / ``output``)
as SEPARATE time-series points, NOT a single row carrying both counts.
So a single (day, model, project) yields up to two points. Cost is
estimated per point from whichever side it carries::

    # input point
    compute_cost_usd(model, input_tokens=N, output_tokens=0,
                     cache_read_tokens=0, cache_creation_tokens=0, ...)
    # output point
    compute_cost_usd(model, input_tokens=0, output_tokens=N,
                     cache_read_tokens=0, cache_creation_tokens=0, ...)

Summing the per-point estimates reconstructs the (model, project, day)
spend estimate. There is **no cache split** in this metric (recon found
no cached-token sub-type), so ``cache_read_tokens`` /
``cache_creation_tokens`` are always 0 — Vertex's authoritative
*cached* spend, if any, shows up in the BigQuery cost stream, not here.

TODO(TM9 Phase A): confirm whether token_count carries a cached-input
sub-type once a live project exists; if it does, add a third point-type
branch mapping it to ``cache_read_tokens`` (and re-confirm
``vertex_rates`` exposes a cached rate for the affected models).

Dedup + cursor (per-DAY watermark)
==================================

``telemetry_records`` carries ``UNIQUE (tenant_id, source_api,
external_id)``. A re-pull of an already-ingested day/point raises
``IntegrityError`` from ``append_telemetry_record``; we catch it and
count the dedup. The cursor is a **per-day watermark** (the last fully-
pulled UTC day): the per-tenant task walks forward day-by-day from the
cursor to "yesterday", so a 15-minute tick usually only ingests the
trailing day(s) once. First run defaults to a small lookback; backfill
is a separate (T3.6-style) entry point not built here.

``external_id`` (contract-pinned)::

    google:vertex_token_usage:{day}:{model}:{project_id}:{type}

with ``-`` for any missing segment. ``day`` is the ``YYYY-MM-DD`` UTC
usage day; ``type`` is ``input`` / ``output``.

Soft-skip statuses
==================

  - ``no_gcp_creds`` — no service-account key sealed (``LookupError``
    from the factory). Soft-skip cleanly (cursor untouched, NO retry) —
    the dispatcher fans out to ALL active tenants, most have no GCP key.
  - ``no_monitoring_access`` — the SA authenticates but lacks
    ``monitoring.read`` on the project (``PermissionDenied`` / 403).
    Soft-skip the whole stream without touching the cursor so a later
    IAM grant re-pulls the window — the Vertex analogue of OpenAI's
    ``no_openai_usage_access``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator, Optional

from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

from vargate_telemetry.celery_app import celery_app
from vargate_telemetry.chain import append_telemetry_record
from vargate_telemetry.db import scheduler_session_scope, session_scope
from vargate_telemetry.metering import increment
from vargate_telemetry.pricing import vertex_rates
from vargate_telemetry.vertex import (
    PermissionDenied,
    TokenUsagePoint,
    gcp_clients_for_tenant,
)

_log = logging.getLogger(__name__)

# Source-API name used in pull_state + telemetry_records for this stream.
# NOTE: must be <= 32 chars (pull_state.source_api limit). This is 18.
SOURCE_API_VERTEX_USAGE = "vertex_token_usage"

# How far back to look on first run when no cursor exists (in days).
DEFAULT_INITIAL_LOOKBACK_DAYS = 2

# Cloud Monitoring publisher token-count metric (recon).
# TODO(TM9 Phase A): confirm the exact metric type string + that
# online_serving is the right surface (vs batch / a separate
# `consumed_token_count`) once a live project exists. The client filters
# on this; the normalize below reads the `type` label off each point.
TOKEN_COUNT_METRIC = (
    "aiplatform.googleapis.com/publisher/online_serving/token_count"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ───────────────────────────────────────────────────────────────────────────
# Cursor I/O — sibling copies of the pull_openai_usage helpers (layout A).
#
# Kept inline per the established posture: this stream's cursor is a
# per-DAY watermark (the last fully-pulled UTC day) and the duplication
# keeps the streams decoupled. The cursor column stores a datetime
# (UTC-midnight of the watermark day) for shape-compatibility with the
# other streams' cursor I/O.
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


def _day_start(dt: datetime) -> datetime:
    """UTC-midnight of the day containing ``dt`` (naive treated as UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _normalize_usage(point: TokenUsagePoint) -> Iterator[dict[str, Any]]:
    """Yield one ``telemetry_records`` insert-kwargs dict per usage point.

    One record per (day, model, project, type) monitoring point so
    per-point cost + dedup + chain granularity line up — the same
    split-per-row posture as ``pull_openai_usage``.

    ``external_id`` (contract-pinned)::

        google:vertex_token_usage:{day}:{model}:{project_id}:{type}

    with ``-`` for any missing segment. ``day`` is the ``YYYY-MM-DD`` UTC
    usage day; ``type`` is ``input`` / ``output``.

    ``content_hash`` is SHA-256 over the canonical JSON of the per-point
    metadata. The cost estimate maps the point's token count to the
    matching side (input OR output) — there is NO cache split in this
    metric, so ``cache_read``/``cache_creation`` are 0. There is **no**
    ``subject_user_id`` and **no** ``user_email`` (Google has no per-user
    attribution).

    NOTE (SCAFFOLD): the field accessors below assume the
    :class:`~vargate_telemetry.vertex.TokenUsagePoint` shape from THE
    CONTRACT (``usage_day``, ``model``, ``project_id``, ``token_type``,
    ``token_count``). The exact attribute names + how the ``type`` label
    surfaces on the point are finalized against the real Cloud Monitoring
    response in Phase A.
    """
    # TODO(TM9 Phase A): confirm TokenUsagePoint exposes `usage_day` as a
    # date/datetime (the point's interval start bucketed to a UTC day,
    # from the monitoring aggregation). Format defensively for now.
    usage_day = getattr(point, "usage_day", None)
    if isinstance(usage_day, datetime):
        day = usage_day.date().isoformat()
        occurred_at = usage_day
    elif isinstance(usage_day, date):
        day = usage_day.isoformat()
        occurred_at = datetime(
            usage_day.year,
            usage_day.month,
            usage_day.day,
            tzinfo=timezone.utc,
        )
    elif usage_day is not None:
        day = str(usage_day)
        occurred_at = _day_to_dt(day)
    else:
        day = "-"
        occurred_at = _now()

    model = point.model or "-"
    project_id = point.project_id or "-"
    # TODO(TM9 Phase A): confirm the label key carrying input/output —
    # recon says label "type"; TokenUsagePoint should expose it as
    # `token_type`. Normalize to lower-case for the external_id segment.
    token_type_raw = getattr(point, "token_type", None)
    token_type = (token_type_raw or "-").lower() if token_type_raw else "-"

    token_count = getattr(point, "token_count", None) or 0

    estimated_cost = _estimate_point_cost(
        model=point.model,
        token_type=token_type,
        token_count=token_count,
        occurred_at=occurred_at,
    )

    point_dict = point.model_dump(mode="json")

    sub_meta: dict[str, Any] = {
        "result": point_dict,
        # Top-level operational fields the dashboard reads directly +
        # the project attribution dim (NO user email — Google has no
        # per-user attribution).
        "usage_day": day,
        "model": point.model,
        "project_id": point.project_id,
        "location": getattr(point, "location", None),
        "token_type": token_type_raw,
        "token_count": token_count,
        # Decimal → str so the JSON metadata is exact (never a float).
        "estimated_cost_usd": (
            str(estimated_cost) if estimated_cost is not None else None
        ),
    }

    canonical = json.dumps(
        sub_meta, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    yield {
        "record_type": "usage",
        "source_api": SOURCE_API_VERTEX_USAGE,
        "external_id": (
            f"google:{SOURCE_API_VERTEX_USAGE}:"
            f"{day}:{model}:{project_id}:{token_type}"
        ),
        "occurred_at": occurred_at,
        "content_hash": hashlib.sha256(canonical).digest(),
        "record_metadata": sub_meta,
        # NO subject_user_id — Google has no per-user attribution.
    }


def _estimate_point_cost(
    *,
    model: Optional[str],
    token_type: str,
    token_count: int,
    occurred_at: datetime,
):
    """Estimate one monitoring point's cost via ``vertex_rates``.

    The point carries EITHER an input count OR an output count (the
    metric splits by ``type``), so map the count to the matching side and
    pass 0 for the other. There is no cache split in this metric, so
    ``cache_read``/``cache_creation`` are always 0.

    Returns ``None`` when the model is null/unknown to the rate card
    (``vertex_rates.compute_cost_usd`` never fakes a number) or the
    point's type is neither input nor output.
    """
    if token_type == "input":
        return vertex_rates.compute_cost_usd(
            model,
            input_tokens=token_count,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            occurred_at=occurred_at,
        )
    if token_type == "output":
        return vertex_rates.compute_cost_usd(
            model,
            input_tokens=0,
            output_tokens=token_count,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            occurred_at=occurred_at,
        )
    # Unknown type — surface the gap, don't fake a number.
    # TODO(TM9 Phase A): if a cached-input `type` exists, add its branch
    # here (→ cache_read_tokens).
    return None


def _day_to_dt(day: str) -> datetime:
    """Parse a ``YYYY-MM-DD`` day string to a UTC-midnight datetime.

    Falls back to ``_now()`` for the ``"-"`` sentinel / unparseable day so
    ``occurred_at`` is always a valid timestamp for the chain.
    """
    try:
        return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return _now()


def _pull_vertex_usage_for_tenant(
    tenant_id: str,
    *,
    clients: Optional[tuple] = None,
) -> dict[str, Any]:
    """Pure-Python pull. Returns counts + status.

    ``clients`` is the ``(billing_client, monitoring_client, meta)``
    tuple from ``vertex.gcp_clients_for_tenant`` — injected by tests,
    built from the sealed SA key in production. Only the monitoring
    client + the project from ``meta`` are used here; the billing client
    is ignored (it belongs to ``pull_vertex_costs``).

    Happy path::

        {"records_pulled": N, "records_deduped": M, "status": "ok"}

    no-creds soft-skip::

        {"records_pulled": 0, "records_deduped": 0,
         "status": "no_gcp_creds"}

    403 soft-skip (SA lacks monitoring.read)::

        {"records_pulled": 0, "records_deduped": 0,
         "status": "no_monitoring_access"}
    """
    if not tenant_id:
        raise ValueError("tenant_id required")

    # 1. Load the per-day watermark in its own transaction so the
    #    monitoring HTTP I/O that follows doesn't hold the DB connection.
    with session_scope(tenant_id) as s:
        cursor = _load_cursor(s, tenant_id, SOURCE_API_VERTEX_USAGE)

    pull_started = _now()
    # `since` is the start of the first day to pull (cursor day, or the
    # lookback day on first run). `until` is now; the client buckets the
    # interval [since, until) into per-day points.
    if cursor is not None:
        since = _day_start(cursor)
    else:
        since = _day_start(
            pull_started - timedelta(days=DEFAULT_INITIAL_LOOKBACK_DAYS)
        )
    until = pull_started

    # 2. Build the clients unless injected.
    owned_clients = clients is None
    if owned_clients:
        try:
            clients = gcp_clients_for_tenant(tenant_id)
        except LookupError:
            # No GCP service-account key sealed — soft-skip (the
            # dispatcher fans out to ALL active tenants; most have no GCP
            # key). Cursor untouched, no retry. Mirrors
            # pull_openai_usage's no_openai_key branch.
            _log.debug(
                "pull_vertex_usage: no GCP SA key sealed for %s", tenant_id
            )
            return {
                "records_pulled": 0,
                "records_deduped": 0,
                "status": "no_gcp_creds",
            }

    _billing_client, monitoring_client, _meta = clients

    records_pulled = 0
    records_deduped = 0

    try:
        try:
            # TODO(TM9 Phase A): confirm list_token_usage's exact
            # signature + that it internally filters on TOKEN_COUNT_METRIC,
            # groups by (model, project, type), and buckets per UTC day.
            # The contract is list_token_usage(since, until) ->
            # list[TokenUsagePoint]; the project is bound on the client at
            # construction (from meta), so it is NOT passed here.
            for point in monitoring_client.list_token_usage(since, until):
                for fields in _normalize_usage(point):
                    try:
                        append_telemetry_record(tenant_id, **fields)
                        increment(tenant_id, SOURCE_API_VERTEX_USAGE)
                        records_pulled += 1
                    except IntegrityError:
                        records_deduped += 1
                        _log.info(
                            "pull_vertex_usage: dedup hit %s/%s",
                            tenant_id,
                            fields["external_id"],
                        )
        except PermissionDenied:
            # SA authenticated but lacks monitoring.read on the project.
            # Soft-skip the whole stream without touching the cursor so a
            # later IAM grant re-pulls the window — the Vertex analogue of
            # OpenAI's no_openai_usage_access.
            _log.info(
                "pull_vertex_usage: 403 no_monitoring_access for %s",
                tenant_id,
            )
            return {
                "records_pulled": 0,
                "records_deduped": 0,
                "status": "no_monitoring_access",
            }

        # 3. Advance the watermark on success (even on dedup-only runs).
        #    The new watermark is the start of TODAY (UTC): every day
        #    strictly before today is now fully pulled, and today's
        #    partial day will be re-pulled (and deduped) on the next tick
        #    until it closes — the per-day analogue of pull_openai_usage's
        #    "cursor advances even on dedup-only runs".
        new_watermark = _day_start(pull_started)
        with session_scope(tenant_id) as s:
            _save_cursor(
                s,
                tenant_id,
                SOURCE_API_VERTEX_USAGE,
                new_watermark,
                status="ok",
            )
    finally:
        if owned_clients:
            _close_clients(clients)

    return {
        "records_pulled": records_pulled,
        "records_deduped": records_deduped,
        "status": "ok",
    }


def _close_clients(clients: Optional[tuple]) -> None:
    """Best-effort close of any clients exposing ``.close()``.

    TODO(TM9 Phase A): confirm whether the google-cloud BigQuery /
    Monitoring clients need explicit close (the openai client did via
    ``client.close()``). google-cloud clients are usually context-
    managed / GC-safe, so this guards rather than assumes.
    """
    if not clients:
        return
    for c in clients:
        close = getattr(c, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover - defensive
                _log.debug("pull_vertex_usage: client close failed")


@celery_app.task(
    bind=True,
    max_retries=3,
    name=(
        "vargate_telemetry.tasks.pull_vertex_usage."
        "pull_vertex_usage_for_tenant"
    ),
)
def pull_vertex_usage_for_tenant(self, tenant_id: str) -> dict[str, Any]:
    """Beat-dispatched per-tenant usage pull. Retries on any exception
    OTHER than the soft-skips (no_gcp_creds / no_monitoring_access return
    cleanly without raising)."""
    try:
        return _pull_vertex_usage_for_tenant(tenant_id)
    except Exception as exc:
        _log.exception("pull_vertex_usage failed for %s", tenant_id)
        raise self.retry(exc=exc, countdown=60)


@celery_app.task(
    name=(
        "vargate_telemetry.tasks.pull_vertex_usage."
        "dispatch_vertex_usage_pulls"
    ),
)
def dispatch_vertex_usage_pulls(region: Optional[str] = None) -> int:
    """Beat fan-out. Enumerate active tenants; queue one usage pull each.

    Mirrors ``pull_openai_usage.dispatch_openai_usage_pulls`` — scheduler-
    role session, all regions by default (the ``region`` arg is an
    explicit override). The per-tenant task soft-skips on no-creds / 403,
    so we don't filter on a (non-persisted) capability flag here.
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
        pull_vertex_usage_for_tenant.delay(row.tenant_id)

    _log.info(
        "dispatch_vertex_usage_pulls: queued %d tenants in region %s",
        len(rows),
        region or "all",
    )
    return len(rows)


__all__ = [
    "DEFAULT_INITIAL_LOOKBACK_DAYS",
    "SOURCE_API_VERTEX_USAGE",
    "TOKEN_COUNT_METRIC",
    "_pull_vertex_usage_for_tenant",
    "dispatch_vertex_usage_pulls",
    "pull_vertex_usage_for_tenant",
]
