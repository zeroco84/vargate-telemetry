# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""OpenAI Admin usage pull task (TM8 Phase B).

OpenAI's analogue of ``pull_admin`` — pulls per-(model, user, key,
project) token usage from the OpenAI Admin ``/usage/completions`` and
``/usage/embeddings`` endpoints and normalizes each grouped result row
into a ``telemetry_records`` chain row.

Two Celery tasks compose the steady-state pipeline (same shape as
``pull_admin`` / ``pull_code_analytics``):

  - ``dispatch_openai_usage_pulls`` — beat fan-out. Enumerates active
    tenants (all regions by default) and queues one
    ``pull_openai_usage_for_tenant.delay(tenant_id)`` per row.
  - ``pull_openai_usage_for_tenant`` — per-tenant. Loads the
    ``(tenant, "openai_admin_usage")`` cursor from ``pull_state``,
    builds a client via ``openai.admin_client_for_tenant``, iterates
    both modalities over ``[cursor, now)``, normalizes each grouped
    ``UsageCompletionsResult`` to a record, and advances the cursor.

The real work lives in ``_pull_openai_usage_for_tenant``, a pure-Python
function accepting an optional ``client`` kwarg — tests inject a
``MockTransport``-backed ``OpenAIAdminClient``; production builds one
from the sealed ``openai_admin_key``.

Grouping
========

Recon §7 requires ``group_by=model,user_id,api_key_id,project_id`` on
every usage pull so each result row carries the dimension tuple Ogma
normalizes on (without ``group_by`` the API emits a single aggregate
row per bucket with every dimension null). The four dims plus the
bucket window form the dedup key (``external_id``).

⚠ Cost mapping — the double-count trap (recon §2.1)
===================================================

``input_tokens`` on the wire is the TOTAL input and equals
``input_uncached_tokens + input_cached_tokens``. Cost is computed from
the split, NEVER from the raw total::

    compute_cost_usd(
        model,
        input_tokens=row.input_uncached_tokens,   # full-rate portion
        cache_read_tokens=row.input_cached_tokens, # cached (half-rate)
        cache_creation_tokens=0,                    # OpenAI has no
                                                    # cache-write charge
        output_tokens=row.output_tokens,
        occurred_at=bucket.start_time,
    )

Passing ``input_tokens=row.input_tokens`` would bill the cached portion
twice (once at full rate via the total, once at the cached rate). The
full token breakdown (audio/image/text sub-splits) is stored verbatim
in ``record_metadata``; only the four billing fields drive cost.

Cross-vendor attribution
=========================

``subject_user_id`` is the OpenAI ``user_id``. So the alias reconciler
can email-match an OpenAI user to an Ogma ``users`` row, the
``record_metadata`` exposes an actor identifier the reconciler's
``ACTOR_KEY_SQL`` COALESCE reads:

  - ``metadata.user_email`` — the OpenAI user's email, resolved from
    the ``openai_users`` side table when present (populated by
    ``pull_openai_projects``). This is the cross-vendor match key.
  - ``metadata.subject_user_id`` — the raw OpenAI ``user_id``, the
    fallback identifier when no email is known (lands unmapped, same
    as Anthropic api-key actors).

Dedup + cursor
==============

``telemetry_records`` carries ``UNIQUE (tenant_id, source_api,
external_id)``. A re-pull of an already-ingested bucket raises
``IntegrityError`` from ``append_telemetry_record``; we catch it and
count the dedup. The cursor (upper bound of what's been pulled)
advances on success even on dedup-only runs — a re-pulled window is a
fully-ingested window. First run defaults to a 1-day lookback;
``backfill`` is a separate (T3.6-style) entry point not built here.

403 soft-skip
=============

Recon §1 found no 403s on a PAYG org, but a scope-limited key in
production can 403. ``InsufficientScope`` is caught and returned as a
``status="no_openai_usage_access"`` dict (cursor untouched) rather than
raised — the dispatch-all-with-soft-skip pattern.
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
    InsufficientScope,
    OpenAIAdminClient,
    UsageBucket,
    UsageCompletionsResult,
    admin_client_for_tenant,
)
from vargate_telemetry.pricing import openai_rates

_log = logging.getLogger(__name__)

# Source-API name used in pull_state + telemetry_records for this stream.
SOURCE_API_OPENAI_USAGE = "openai_admin_usage"

# Modalities to pull. /usage/completions is the token-rich main stream;
# /usage/embeddings is structurally identical (recon §1) — empty in the
# probe window but the same envelope, so the same normalize handles it.
DEFAULT_MODALITIES = ("completions", "embeddings")

# Recon §7: always request the full dimension tuple so each result row
# is per-(model, user, key, project) — the grain we dedup + attribute on.
USAGE_GROUP_BY = ["model", "user_id", "api_key_id", "project_id"]

# How far back to look on first run when no cursor exists.
DEFAULT_INITIAL_LOOKBACK_DAYS = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch(dt: datetime) -> int:
    """Unix-epoch seconds for an external_id segment (stable across re-pulls)."""
    return int(dt.timestamp())


# ───────────────────────────────────────────────────────────────────────────
# Cursor I/O — sibling copies of the pull_admin helpers.
#
# Kept inline (not refactored into a shared module) per layout decision
# A and the established posture in pull_code_analytics / pull_compliance:
# each stream's cursor semantics differ subtly and the duplication keeps
# the streams decoupled.
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


def _load_email_map(tenant_id: str) -> dict[str, str]:
    """Build ``{openai_user_id: email}`` from the ``openai_users`` side table.

    Populated by ``pull_openai_projects``. Used to resolve a usage row's
    ``user_id`` to the email the cross-vendor alias reconciler matches
    on. Read under an RLS-bound session so we only see this tenant's
    rows. Returns ``{}`` when the side table is empty (coarser tier, or
    the projects pull hasn't run yet) — the usage record then carries
    only the raw ``user_id`` as its identifier and lands unmapped.
    """
    with session_scope(tenant_id) as s:
        rows = s.execute(
            sql_text(
                "SELECT openai_user_id, email FROM openai_users "
                "WHERE email IS NOT NULL"
            )
        ).all()
    return {r.openai_user_id: r.email for r in rows if r.email}


def _normalize_usage(
    bucket: UsageBucket,
    *,
    modality: str,
    email_map: dict[str, str],
) -> Iterator[dict[str, Any]]:
    """Yield one ``telemetry_records`` insert-kwargs dict per result row.

    One record per ``bucket.results[i]`` grouped row, so per-(date,
    model, user, key, project) cost + dedup + chain granularity all
    line up — same split-per-breakdown posture as ``pull_admin``.

    ``external_id`` (recon-pinned)::

        openai:openai_admin_usage:{start}:{end}:{model}:{project_id}:{api_key_id}:{user_id}

    with ``-`` for any missing segment. ``start`` / ``end`` are the
    bucket window as integer epoch seconds (stable across re-pulls).

    ``content_hash`` is SHA-256 over the canonical JSON of the
    per-row wrapper ``{start, end, modality, result: <one row>}`` — a
    refreshed token count changes that record's hash without disturbing
    siblings.

    Empty-result buckets (a day with no usage) still emit a sentinel
    record so the cursor advances and we don't re-pull an empty window
    forever — same posture as ``pull_admin``. The sentinel external_id
    omits the modality (it's not in the pinned format), so an empty
    completions bucket and an empty embeddings bucket for the same window
    collide on dedup: one inserts, the other dedups. That's fine — a
    single sentinel per window is enough to advance the (per-source_api)
    cursor, which is shared across modalities.

    Cost (``estimated_cost_usd`` in metadata) heeds the §2.1
    double-count trap: ``input_tokens=input_uncached_tokens``,
    ``cache_read_tokens=input_cached_tokens``, ``cache_creation=0``.
    """
    start_epoch = _epoch(bucket.start_time)
    end_epoch = _epoch(bucket.end_time)
    window = {
        "start_time": bucket.start_time.isoformat(),
        "end_time": bucket.end_time.isoformat(),
        "modality": modality,
    }

    if not bucket.results:
        sub_meta = {**window, "result": None}
        canonical = json.dumps(
            sub_meta, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        yield {
            "record_type": "usage",
            "source_api": SOURCE_API_OPENAI_USAGE,
            "external_id": (
                f"openai:{SOURCE_API_OPENAI_USAGE}:"
                f"{start_epoch}:{end_epoch}:-:-:-:-"
            ),
            "occurred_at": bucket.start_time,
            "content_hash": hashlib.sha256(canonical).digest(),
            "record_metadata": sub_meta,
        }
        return

    for result in bucket.results:
        yield _normalize_result(
            result,
            bucket=bucket,
            modality=modality,
            window=window,
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            email_map=email_map,
        )


def _normalize_result(
    result: UsageCompletionsResult,
    *,
    bucket: UsageBucket,
    modality: str,
    window: dict[str, Any],
    start_epoch: int,
    end_epoch: int,
    email_map: dict[str, str],
) -> dict[str, Any]:
    """Normalize a single grouped usage result row → insert kwargs."""
    result_dict = result.model_dump(mode="json")

    model = result.model or "-"
    project_id = result.project_id or "-"
    api_key_id = result.api_key_id or "-"
    user_id = result.user_id or "-"

    # §2.1 double-count trap: derive cost from the uncached + cached
    # split, NEVER from the raw input_tokens total.
    estimated_cost = openai_rates.compute_cost_usd(
        result.model,
        input_tokens=result.input_uncached_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.input_cached_tokens,
        cache_creation_tokens=0,
        occurred_at=bucket.start_time,
    )

    # Cross-vendor attribution identifier. Prefer the resolved email
    # (the reconciler's match key); fall back to the raw user_id.
    email = email_map.get(result.user_id) if result.user_id else None

    sub_meta: dict[str, Any] = {
        **window,
        "result": result_dict,
        # Operational fields the dashboard + reconciler read directly,
        # surfaced top-level so SQL doesn't have to dig into `result`.
        "subject_user_id": result.user_id,
        "model": result.model,
        "project_id": result.project_id,
        "api_key_id": result.api_key_id,
        # Decimal → str so the JSON metadata is exact (never a float).
        "estimated_cost_usd": (
            str(estimated_cost) if estimated_cost is not None else None
        ),
    }
    if email is not None:
        sub_meta["user_email"] = email

    canonical = json.dumps(
        sub_meta, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    return {
        "record_type": "usage",
        "source_api": SOURCE_API_OPENAI_USAGE,
        "external_id": (
            f"openai:{SOURCE_API_OPENAI_USAGE}:"
            f"{start_epoch}:{end_epoch}:"
            f"{model}:{project_id}:{api_key_id}:{user_id}"
        ),
        "occurred_at": bucket.start_time,
        "content_hash": hashlib.sha256(canonical).digest(),
        "record_metadata": sub_meta,
        "subject_user_id": result.user_id,
    }


def _pull_openai_usage_for_tenant(
    tenant_id: str,
    *,
    client: Optional[OpenAIAdminClient] = None,
    modalities: tuple[str, ...] = DEFAULT_MODALITIES,
) -> dict[str, Any]:
    """Pure-Python pull. Returns counts + status.

    Happy path::

        {"records_pulled": N, "records_deduped": M, "status": "ok"}

    403 soft-skip (scope-limited key)::

        {"records_pulled": 0, "records_deduped": 0,
         "status": "no_openai_usage_access"}
    """
    if not tenant_id:
        raise ValueError("tenant_id required")

    # 1. Load cursor in its own transaction so the HTTP I/O that
    #    follows doesn't hold the DB connection.
    with session_scope(tenant_id) as s:
        cursor = _load_cursor(s, tenant_id, SOURCE_API_OPENAI_USAGE)

    pull_started = _now()
    start_time = cursor or (
        pull_started - timedelta(days=DEFAULT_INITIAL_LOOKBACK_DAYS)
    )

    # 2. Resolve user_id → email for cross-vendor attribution (best
    #    effort; empty when the projects pull hasn't run or the tier
    #    exposes no users).
    email_map = _load_email_map(tenant_id)

    # 3. Build the client unless one was injected.
    owned_client = client is None
    if owned_client:
        client = admin_client_for_tenant(tenant_id)

    records_pulled = 0
    records_deduped = 0

    try:
        for modality in modalities:
            try:
                for bucket in client.list_usage(
                    modality=modality,
                    start_time=start_time,
                    end_time=pull_started,
                    group_by=USAGE_GROUP_BY,
                ):
                    for fields in _normalize_usage(
                        bucket, modality=modality, email_map=email_map
                    ):
                        try:
                            append_telemetry_record(tenant_id, **fields)
                            increment(tenant_id, SOURCE_API_OPENAI_USAGE)
                            records_pulled += 1
                        except IntegrityError:
                            records_deduped += 1
                            _log.info(
                                "pull_openai_usage: dedup hit %s/%s",
                                tenant_id,
                                fields["external_id"],
                            )
            except InsufficientScope:
                # A scope-limited key. Soft-skip the WHOLE stream — if
                # the org can't read /usage/completions it can't read
                # /usage/embeddings either; bail without touching the
                # cursor so a later key upgrade re-pulls the window.
                _log.info(
                    "pull_openai_usage: 403 no_openai_usage_access for %s",
                    tenant_id,
                )
                return {
                    "records_pulled": 0,
                    "records_deduped": 0,
                    "status": "no_openai_usage_access",
                }

        # 4. Advance the cursor on success (even on dedup-only runs).
        with session_scope(tenant_id) as s:
            _save_cursor(
                s,
                tenant_id,
                SOURCE_API_OPENAI_USAGE,
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
        "vargate_telemetry.tasks.pull_openai_usage."
        "pull_openai_usage_for_tenant"
    ),
)
def pull_openai_usage_for_tenant(self, tenant_id: str) -> dict[str, Any]:
    """Beat-dispatched per-tenant usage pull. Retries on any exception
    OTHER than the 403 soft-skip (which returns cleanly)."""
    try:
        return _pull_openai_usage_for_tenant(tenant_id)
    except Exception as exc:
        _log.exception("pull_openai_usage failed for %s", tenant_id)
        raise self.retry(exc=exc, countdown=60)


@celery_app.task(
    name=(
        "vargate_telemetry.tasks.pull_openai_usage."
        "dispatch_openai_usage_pulls"
    ),
)
def dispatch_openai_usage_pulls(region: Optional[str] = None) -> int:
    """Beat fan-out. Enumerate active tenants; queue one usage pull each.

    Mirrors ``pull_admin.dispatch_admin_pulls`` — scheduler-role
    session, all regions by default (the ``region`` arg is an explicit
    override; the old VARGATE_REGION=us default silently skipped eu
    tenants, TM5 T5.0). The per-tenant task soft-skips on 403, so we
    don't filter on a (non-persisted) capability flag here.
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
        pull_openai_usage_for_tenant.delay(row.tenant_id)

    _log.info(
        "dispatch_openai_usage_pulls: queued %d tenants in region %s",
        len(rows),
        region or "all",
    )
    return len(rows)


__all__ = [
    "DEFAULT_INITIAL_LOOKBACK_DAYS",
    "DEFAULT_MODALITIES",
    "SOURCE_API_OPENAI_USAGE",
    "USAGE_GROUP_BY",
    "_pull_openai_usage_for_tenant",
    "dispatch_openai_usage_pulls",
    "pull_openai_usage_for_tenant",
]
