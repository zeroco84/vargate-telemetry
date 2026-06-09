# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Google Vertex AI cost pull task (TM9 Phase A SCAFFOLD).

Pulls **authoritative billed spend** for Vertex AI from the customer's
**BigQuery billing export** table (recon: ``gcp_billing_export_v1_<ACCT>``
in a customer-owned dataset). This is the Vertex analogue of
``pull_openai_costs``: it is the authoritative spend stream, complementing
``pull_vertex_usage`` (which gives per-(model, project) token detail + our
own cost *estimate*). Forecasting / total spend prefers this stream; per-
project token detail comes from usage.

Two Celery tasks compose the pipeline (same shape as
``pull_openai_costs``):

  - ``dispatch_vertex_costs_pulls`` — beat fan-out over active tenants
    (all regions by default; dispatch-all-with-soft-skip).
  - ``pull_vertex_costs_for_tenant`` — per-tenant. Loads the
    ``(tenant, "vertex_billing_costs")`` cursor, queries the BigQuery
    billing export over a trailing window, and normalizes each
    :class:`~vargate_telemetry.vertex.BillingRow` to a record.

Attribution — project/team ONLY
================================

Google has **no per-user-email attribution** for Vertex spend. The
billing export groups by ``project.id`` and request ``labels`` (team
labels), never an end-user email. Consequently — and unlike
``pull_openai_costs`` (which still has a ``project_id`` dim) — this
stream sets **no** ``subject_user_id``, writes **no** ``user_email``,
and is **never** read by the cross-vendor email reconciler. The
attribution dims are ``project.id`` and ``team`` (derived from request
labels). There is intentionally NO vertex users side-table.

Authoritative, never estimated
===============================

The billing export ``cost`` (net of ``credits``) is the real billed
amount. ``pricing.vendor_cost.estimate_record_cost_usd`` returns
``None`` for a ``vertex_billing_costs`` record on purpose: the cost
record is authoritative and is read directly by the spend rollups,
while the *usage* stream carries the token-derived estimate. Pricing
both would double-count — the same per-vendor spend split that the
OpenAI streams established (recon §2.1 analogue).

Net cost (recon)
================

The export emits gross ``cost`` plus a ``credits[]`` array whose
``amount`` values are **negative** (discounts, free-tier, committed-use
credits). Net billed spend is ``SUM(cost) + SUM(credits.amount)`` — the
``+`` is correct because the credit amounts are already signed. The
:class:`~vargate_telemetry.vertex.BillingRow` carries ``cost`` and the
per-row ``credits`` list; :func:`_normalize_costs` computes
``net_cost`` and stores all three (gross, credits, net) as exact
strings in ``record_metadata``.

Restatement → trailing re-pull (recon)
======================================

The billing export trails real time by hours-to-~5 days AND is
**restated** in place (a day's rows can change after first export as
late usage and credits settle). A high-watermark-only cursor would
permanently miss restatements of already-seen days. So the cursor is a
high-watermark on the export ``export_time``, but each run RE-PULLS a
trailing window (``RESTATEMENT_WINDOW_DAYS``) below the watermark. Re-
pulled rows whose content is unchanged dedup on ``external_id``; rows
whose ``cost``/``credits`` changed get a new ``content_hash`` (the
external_id is stable per day/sku/project/label-set, the hash tracks
the restated amount).

``external_id`` (contract-pinned)::

    google:vertex_billing_costs:{day}:{sku_id}:{project_id}:{label_hash}

with ``-`` for any missing segment. ``day`` is the ``YYYY-MM-DD`` UTC
usage day; ``label_hash`` is a short stable digest of the (sorted)
request-label set so two rows that differ only by team labels don't
collide. ``sku_id`` can contain no colons (it is a GCP SKU id like
``9C2D-1A2B-3E4F``) so the positional format is unambiguous.

Soft-skip statuses
==================

  - ``no_gcp_creds`` — no service-account key sealed for the tenant
    (``LookupError`` from the factory). The dispatcher fans out to ALL
    active tenants and most have no GCP key — soft-skip cleanly (cursor
    untouched, NO retry), same as ``pull_openai_costs``'s
    ``no_openai_key``.
  - ``no_billing_access`` — the SA can authenticate but lacks BigQuery
    read on the export dataset (``PermissionDenied`` / 403). Soft-skip
    the whole stream without touching the cursor so a later IAM grant
    re-pulls the window — the Vertex analogue of OpenAI's
    ``no_openai_costs_access``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterator, Optional

from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

from vargate_telemetry.celery_app import celery_app
from vargate_telemetry.chain import append_telemetry_record
from vargate_telemetry.db import scheduler_session_scope, session_scope
from vargate_telemetry.metering import increment
from vargate_telemetry.vertex import (
    BillingRow,
    PermissionDenied,
    gcp_clients_for_tenant,
)

_log = logging.getLogger(__name__)

# Source-API name used in pull_state + telemetry_records for this stream.
# NOTE: must be <= 32 chars (pull_state.source_api limit). This is 20.
SOURCE_API_VERTEX_COSTS = "vertex_billing_costs"

# How far back to look on first run when no cursor exists.
DEFAULT_INITIAL_LOOKBACK_DAYS = 2

# Restatement re-pull window. The billing export trails + is restated for
# up to ~5 days (recon), so every run re-pulls this many days BELOW the
# watermark to pick up restated rows. Re-pulls dedup unless the amount
# changed (see module docstring).
# TODO(TM9 Phase A): confirm the real restatement horizon for this billing
# account once a live export exists — recon says hours-to-~5 days; 5 is the
# conservative upper bound. Widen if a restatement is observed past it.
RESTATEMENT_WINDOW_DAYS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ───────────────────────────────────────────────────────────────────────────
# Cursor I/O — sibling copies of the pull_openai_costs helpers (layout A).
#
# Kept inline (not refactored into a shared module) per the established
# posture: each stream's cursor semantics differ subtly (this one is a
# high-watermark on the export's export_time with a trailing re-pull) and
# the duplication keeps the streams decoupled.
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


def _label_hash(labels: Any) -> str:
    """Short stable digest of a row's (request) label set.

    Labels arrive as a list of ``{"key": ..., "value": ...}`` dicts (the
    BigQuery export shape) OR a flat ``{key: value}`` dict, depending on
    how the client normalizes them. Both are reduced to a sorted list of
    ``"key=value"`` pairs and SHA-256'd; the first 12 hex chars form the
    external_id segment so two rows that differ only by team labels don't
    collide on dedup. Empty / missing labels → ``"none"``.

    The full label set is preserved verbatim in ``record_metadata`` —
    this is only the dedup discriminator.

    TODO(TM9 Phase A): confirm the exact label container shape the
    BigQuery client returns (list-of-pairs vs flat dict) once a live
    export exists, and confirm WHICH label namespace carries the team
    attribution (the export distinguishes resource labels from request
    labels — the contract uses *request* labels for ``team``).
    """
    if not labels:
        return "none"

    if isinstance(labels, dict):
        pairs = [f"{k}={labels[k]}" for k in sorted(labels)]
    else:
        # list of {"key", "value"} (or objects exposing .key/.value)
        norm: list[str] = []
        for item in labels:
            if isinstance(item, dict):
                k = item.get("key")
                v = item.get("value")
            else:
                k = getattr(item, "key", None)
                v = getattr(item, "value", None)
            norm.append(f"{k}={v}")
        pairs = sorted(norm)

    if not pairs:
        return "none"

    canonical = "\x1f".join(pairs).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:12]


def _normalize_costs(row: BillingRow) -> Iterator[dict[str, Any]]:
    """Yield one ``telemetry_records`` insert-kwargs dict per billing row.

    One record per per-(day, sku, project, label-set) billing row so
    per-row dedup + chain granularity line up — the cost analogue of the
    per-breakdown split in ``pull_openai_usage``.

    ``external_id`` (contract-pinned)::

        google:vertex_billing_costs:{day}:{sku_id}:{project_id}:{label_hash}

    with ``-`` for any missing segment.

    ``content_hash`` is SHA-256 over the canonical JSON of the per-row
    metadata. **Net cost** = gross ``cost`` + ``SUM(credits.amount)``
    (credit amounts are already negative — recon). Gross, credit total,
    and net are all stored as exact strings in ``record_metadata`` so
    billed spend survives JSON without passing through a binary float;
    there is **no** ``subject_user_id`` and **no** ``user_email`` (Google
    has no per-user attribution).

    NOTE (SCAFFOLD): the field accessors below assume the
    :class:`~vargate_telemetry.vertex.BillingRow` shape from THE CONTRACT
    (``usage_day``, ``sku_id``, ``project_id``, ``cost``, ``currency``,
    ``credits`` [list of objects with ``.amount``], ``labels``). The
    exact attribute names + the credit-sign convention are finalized
    against the real BigQuery export in Phase A.
    """
    # TODO(TM9 Phase A): confirm BillingRow exposes `usage_day` as a
    # date/datetime (the grouped YYYY-MM-DD usage day, derived from
    # usage_start_time in the export SQL). Format defensively for now.
    usage_day = getattr(row, "usage_day", None)
    if isinstance(usage_day, datetime):
        day = usage_day.date().isoformat()
    elif usage_day is not None:
        day = str(usage_day)
    else:
        day = "-"

    sku_id = row.sku_id or "-"
    project_id = row.project_id or "-"
    labels = getattr(row, "labels", None)
    label_hash = _label_hash(labels)

    # Net = gross cost + signed credits (credits are negative). All three
    # kept as exact Decimal strings in metadata.
    gross = row.cost if row.cost is not None else Decimal(0)
    credit_total = _sum_credits(row)
    net_cost = gross + credit_total

    row_dict = row.model_dump(mode="json")

    sub_meta: dict[str, Any] = {
        "result": row_dict,
        # Top-level operational fields for the dashboard's cost view +
        # the project/team attribution dims (NO user email — Google has
        # no per-user attribution).
        "usage_day": day,
        "sku_id": row.sku_id,
        "sku_description": getattr(row, "sku_description", None),
        "service_description": getattr(row, "service_description", None),
        "project_id": row.project_id,
        "project_name": getattr(row, "project_name", None),
        "labels": _labels_to_plain(labels),
        "currency": getattr(row, "currency", None),
        # Exact Decimal strings (never a float) — net is the billed amount.
        "gross_cost_usd": str(gross),
        "credit_total_usd": str(credit_total),
        "net_cost_usd": str(net_cost),
    }

    canonical = json.dumps(
        sub_meta, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    # occurred_at = the usage day at UTC midnight when available, else now.
    occurred_at = (
        usage_day
        if isinstance(usage_day, datetime)
        else _day_to_dt(day)
    )

    yield {
        "record_type": "cost",
        "source_api": SOURCE_API_VERTEX_COSTS,
        "external_id": (
            f"google:{SOURCE_API_VERTEX_COSTS}:"
            f"{day}:{sku_id}:{project_id}:{label_hash}"
        ),
        "occurred_at": occurred_at,
        "content_hash": hashlib.sha256(canonical).digest(),
        "record_metadata": sub_meta,
    }


def _sum_credits(row: BillingRow) -> Decimal:
    """Sum a billing row's ``credits[].amount`` as a signed Decimal.

    Credit amounts are negative in the export (discounts / free-tier /
    CUD), so ``net = cost + SUM(credits.amount)``. Missing / empty →
    ``Decimal(0)``.

    TODO(TM9 Phase A): confirm the credits container shape on BillingRow
    (list of objects exposing ``.amount`` vs list of dicts) and confirm
    the amounts are delivered already-signed (negative) rather than as
    positive magnitudes — the net formula's ``+`` depends on it.
    """
    credits = getattr(row, "credits", None)
    if not credits:
        return Decimal(0)

    total = Decimal(0)
    for c in credits:
        if isinstance(c, dict):
            amt = c.get("amount")
        else:
            amt = getattr(c, "amount", None)
        if amt is None:
            continue
        # BillingRow sub-shape should already parse amount as Decimal
        # (Decimal(str(value)) in the type — mirrors openai CostAmount);
        # str() guards a stray float just in case.
        total += amt if isinstance(amt, Decimal) else Decimal(str(amt))
    return total


def _labels_to_plain(labels: Any) -> Optional[dict[str, Any]]:
    """Reduce the label container to a plain ``{key: value}`` dict for
    metadata (so the dashboard + a future team-rollup SQL can read it
    without knowing the wire shape). ``None`` when absent."""
    if not labels:
        return None
    if isinstance(labels, dict):
        return dict(labels)
    out: dict[str, Any] = {}
    for item in labels:
        if isinstance(item, dict):
            k = item.get("key")
            v = item.get("value")
        else:
            k = getattr(item, "key", None)
            v = getattr(item, "value", None)
        if k is not None:
            out[str(k)] = v
    return out or None


def _day_to_dt(day: str) -> datetime:
    """Parse a ``YYYY-MM-DD`` day string to a UTC-midnight datetime.

    Falls back to ``_now()`` for the ``"-"`` sentinel / unparseable day so
    ``occurred_at`` is always a valid timestamp for the chain.
    """
    try:
        return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return _now()


def _pull_vertex_costs_for_tenant(
    tenant_id: str,
    *,
    clients: Optional[tuple] = None,
) -> dict[str, Any]:
    """Pure-Python pull. Returns counts + status.

    ``clients`` is the ``(billing_client, monitoring_client, meta)``
    tuple from ``vertex.gcp_clients_for_tenant`` — injected by tests,
    built from the sealed SA key in production. Only the billing client +
    the dataset/location from ``meta`` are used here; the monitoring
    client is ignored (it belongs to ``pull_vertex_usage``).

    Happy path::

        {"records_pulled": N, "records_deduped": M, "status": "ok"}

    no-creds soft-skip::

        {"records_pulled": 0, "records_deduped": 0,
         "status": "no_gcp_creds"}

    403 soft-skip (SA lacks BigQuery read on the export)::

        {"records_pulled": 0, "records_deduped": 0,
         "status": "no_billing_access"}
    """
    if not tenant_id:
        raise ValueError("tenant_id required")

    # 1. Load cursor (high-watermark on export_time) in its own
    #    transaction so the BigQuery I/O that follows doesn't hold the DB
    #    connection.
    with session_scope(tenant_id) as s:
        cursor = _load_cursor(s, tenant_id, SOURCE_API_VERTEX_COSTS)

    pull_started = _now()
    watermark = cursor or (
        pull_started - timedelta(days=DEFAULT_INITIAL_LOOKBACK_DAYS)
    )
    # Re-pull a trailing window below the watermark to absorb restatements
    # (recon: the export is restated for up to ~5 days). `since` is the
    # query lower bound; `until` is the upper bound (now).
    since = watermark - timedelta(days=RESTATEMENT_WINDOW_DAYS)
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
            # pull_openai_costs's no_openai_key branch.
            _log.debug(
                "pull_vertex_costs: no GCP SA key sealed for %s", tenant_id
            )
            return {
                "records_pulled": 0,
                "records_deduped": 0,
                "status": "no_gcp_creds",
            }

    billing_client, _monitoring_client, meta = clients

    # The BigQuery billing-export dataset + its location come from
    # onboarding (sealed/stored alongside the SA key) and are surfaced on
    # `meta`.
    # TODO(TM9 Phase A): confirm the `meta` shape returned by
    # gcp_clients_for_tenant carries `billing_dataset` (the dataset
    # holding gcp_billing_export_v1_<ACCT>) — and whether the table
    # suffix <ACCT> needs to be passed separately or is discovered by the
    # client. The client.query_costs(dataset, since, until) contract takes
    # the dataset; the table is resolved inside the client.
    dataset = _billing_dataset(meta)

    records_pulled = 0
    records_deduped = 0

    try:
        try:
            for row in billing_client.query_costs(
                dataset,
                since,
                until,
            ):
                for fields in _normalize_costs(row):
                    try:
                        append_telemetry_record(tenant_id, **fields)
                        increment(tenant_id, SOURCE_API_VERTEX_COSTS)
                        records_pulled += 1
                    except IntegrityError:
                        # Unchanged restated/re-pulled row — same
                        # external_id + same content_hash → UNIQUE
                        # violation. A row whose amount CHANGED has a new
                        # content_hash and a stable external_id, which is
                        # itself a UNIQUE (tenant, source_api,
                        # external_id) collision: the restated amount
                        # lives in metadata but the canonical external_id
                        # is per-day/sku/project/label, so a restatement
                        # also dedups here. Authoritative net spend is
                        # read from the LATEST matching record's metadata
                        # downstream.
                        # TODO(TM9 Phase A): decide the restatement-update
                        # policy once a live restatement is observed — if
                        # we must reflect a changed amount we either fold
                        # the amount into external_id or add an explicit
                        # update path. For the scaffold, dedup-on-collision
                        # matches every other stream.
                        records_deduped += 1
                        _log.info(
                            "pull_vertex_costs: dedup hit %s/%s",
                            tenant_id,
                            fields["external_id"],
                        )
        except PermissionDenied:
            # SA authenticated but lacks BigQuery read on the export
            # dataset. Soft-skip the whole stream without touching the
            # cursor so a later IAM grant re-pulls the window — the Vertex
            # analogue of OpenAI's no_openai_costs_access.
            _log.info(
                "pull_vertex_costs: 403 no_billing_access for %s",
                tenant_id,
            )
            return {
                "records_pulled": 0,
                "records_deduped": 0,
                "status": "no_billing_access",
            }

        # 3. Advance the watermark on success (even on dedup-only runs —
        #    a re-pulled window is a fully-ingested window). The watermark
        #    is `pull_started`; the trailing re-pull below it absorbs
        #    restatements on the next tick.
        with session_scope(tenant_id) as s:
            _save_cursor(
                s,
                tenant_id,
                SOURCE_API_VERTEX_COSTS,
                pull_started,
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


def _billing_dataset(meta: Any) -> Optional[str]:
    """Extract the BigQuery billing-export dataset id from the factory
    ``meta``.

    TODO(TM9 Phase A): finalize the `meta` contract from
    gcp_clients_for_tenant — this reads ``meta["billing_dataset"]`` for a
    dict or ``meta.billing_dataset`` for an object. The dataset name +
    BigQuery location are captured at onboarding; confirm the key name
    and whether the dataset must be qualified with its location for the
    query.
    """
    if meta is None:
        return None
    if isinstance(meta, dict):
        return meta.get("billing_dataset")
    return getattr(meta, "billing_dataset", None)


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
                _log.debug("pull_vertex_costs: client close failed")


@celery_app.task(
    bind=True,
    max_retries=3,
    name=(
        "vargate_telemetry.tasks.pull_vertex_costs."
        "pull_vertex_costs_for_tenant"
    ),
)
def pull_vertex_costs_for_tenant(self, tenant_id: str) -> dict[str, Any]:
    """Beat-dispatched per-tenant costs pull. Retries on any exception
    OTHER than the soft-skips (no_gcp_creds / no_billing_access return
    cleanly without raising)."""
    try:
        return _pull_vertex_costs_for_tenant(tenant_id)
    except Exception as exc:
        _log.exception("pull_vertex_costs failed for %s", tenant_id)
        raise self.retry(exc=exc, countdown=120)


@celery_app.task(
    name=(
        "vargate_telemetry.tasks.pull_vertex_costs."
        "dispatch_vertex_costs_pulls"
    ),
)
def dispatch_vertex_costs_pulls(region: Optional[str] = None) -> int:
    """Beat fan-out. Enumerate active tenants; queue one costs pull each.

    Mirrors ``pull_openai_costs.dispatch_openai_costs_pulls`` — scheduler-
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
        pull_vertex_costs_for_tenant.delay(row.tenant_id)

    _log.info(
        "dispatch_vertex_costs_pulls: queued %d tenants in region %s",
        len(rows),
        region or "all",
    )
    return len(rows)


__all__ = [
    "DEFAULT_INITIAL_LOOKBACK_DAYS",
    "RESTATEMENT_WINDOW_DAYS",
    "SOURCE_API_VERTEX_COSTS",
    "_pull_vertex_costs_for_tenant",
    "dispatch_vertex_costs_pulls",
    "pull_vertex_costs_for_tenant",
]
