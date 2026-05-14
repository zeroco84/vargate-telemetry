# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Celery application instance for Telemetry workers (T1.3)."""

import os

from celery import Celery
from celery.signals import worker_process_shutdown

celery_app = Celery(
    "vargate_telemetry",
    broker=os.environ["CELERY_BROKER_URL"],          # redis://redis:6379/1
    backend=os.environ["CELERY_RESULT_BACKEND"],     # redis://redis:6379/2
    # TM1: mcp_server.tasks lives outside the vargate_telemetry tree
    # so it can be carved into its own package later (Apache 2.0 vs
    # BSL-1.1). Include both module paths so the worker discovers
    # both task sets.
    include=[
        "vargate_telemetry.tasks",
        "mcp_server.tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # The worker listens on -Q telemetry,default; route un-specified
    # tasks to `default` so they actually get picked up. Without this,
    # `add.delay(...)` lands in the Celery-stock `celery` queue and
    # nothing consumes it.
    task_default_queue="default",
)

# Beat schedule. Beat runs scheduled tasks; the worker executes them.
# T2.3: the metering flush moves Redis counters into Postgres every
# 60 seconds. T3+ will add more schedules here.
celery_app.conf.beat_schedule = {
    "flush-meter-counters": {
        "task": "vargate_telemetry.tasks.metering.flush_counters",
        "schedule": 60.0,  # every 60 seconds
        "options": {"queue": "default"},
    },
    # T3.5: every 15 minutes, fan out one pull task per active tenant
    # in the current region. The dispatcher uses the read-only
    # `vargate_scheduler` role to enumerate `tenants`; per-tenant
    # tasks run under `vargate_app` with RLS enforced per session.
    "dispatch-admin-pulls": {
        "task": "vargate_telemetry.tasks.pull_admin.dispatch_admin_pulls",
        "schedule": 900.0,  # every 15 minutes
        "options": {"queue": "default"},
    },
    # T5.3: parallel to dispatch-admin-pulls but for the Compliance
    # Activity Feed stream. Same 15-minute cadence, same per-tenant
    # cursor model in `pull_state` (source_api='compliance_activities'),
    # separate task name + cursor row so the two streams advance
    # independently.
    "dispatch-compliance-activity-pulls": {
        "task": (
            "vargate_telemetry.tasks.pull_compliance."
            "dispatch_compliance_activity_pulls"
        ),
        "schedule": 900.0,  # every 15 minutes
        "options": {"queue": "default"},
    },
    # T5.3: parallel to dispatch-compliance-activity-pulls but for the
    # Content endpoints (chats, files, projects). Every tenant currently
    # raises NotConfigured because no tenant has a sealed Compliance
    # Access Key yet — the dispatcher catches and logs+skips. The
    # schedule is live now so when a future sprint activates content
    # ingest, it picks up on the next 15-minute tick without a beat-
    # schedule change.
    "dispatch-compliance-content-pulls": {
        "task": (
            "vargate_telemetry.tasks.pull_compliance."
            "dispatch_compliance_content_pulls"
        ),
        "schedule": 900.0,  # every 15 minutes
        "options": {"queue": "default"},
    },
    # T5.4: Code Analytics stream. Daily aggregation (one
    # `starting_at` per call), but the dispatcher fans out at the
    # same 15-minute cadence as the other streams — the per-tenant
    # task walks forward day-by-day from the persisted cursor, so a
    # 15-minute tick usually only ingests "yesterday" once. Faster
    # cadence keeps the catchup loop responsive when a new tenant
    # onboards.
    "dispatch-code-analytics-pulls": {
        "task": (
            "vargate_telemetry.tasks.pull_code_analytics."
            "dispatch_code_analytics_pulls"
        ),
        "schedule": 900.0,  # every 15 minutes
        "options": {"queue": "default"},
    },
    # TM2 Phase C4: re-fetch the bridge JWK from Ogma's well-known
    # endpoint every 24h so key rotation propagates without a
    # redeploy. The MCP server's lifespan primes the cache at boot;
    # this task keeps it fresh while the server is long-running.
    # On failure the task logs but does NOT raise — the stale cache
    # stays warm and the next tick retries.
    "refresh-bridge-jwk": {
        "task": "mcp_server.tasks.refresh_bridge_jwk.refresh_bridge_jwk",
        "schedule": 86400.0,  # 24 hours
        "options": {"queue": "default"},
    },
}

# Alias so `celery -A vargate_telemetry.celery_app worker` (which looks up
# the default attribute name `app`) resolves the same instance.
app = celery_app


# ───────────────────────────────────────────────────────────────────────────
# T4.8.1: Prometheus multi-process cleanup hook.
#
# When a prefork worker child exits (worker shutdown, scale-down, OOM),
# its files in PROMETHEUS_MULTIPROC_DIR linger. For Counters and
# Histograms that's actually correct — the sum across alive + dead
# workers is the right aggregate, and `multiprocess.MultiProcessCollector`
# reads all files regardless. For Gauges (T5+ will ship some), the
# default `multiprocess_mode="all"` would over-count.
#
# `multiprocess.mark_process_dead(pid)` removes the worker's Gauge
# files (and only the Gauge files) so subsequent aggregations skip
# them. Calling it now means the first Gauge ships in T5 already
# benefits — no follow-up wiring needed.
#
# No-op when PROMETHEUS_MULTIPROC_DIR isn't set (unit tests, dev
# without compose) — `mark_process_dead` checks the env var itself
# before touching the filesystem.
# ───────────────────────────────────────────────────────────────────────────


@worker_process_shutdown.connect
def _cleanup_prometheus_multiproc(pid: int, exitcode: int, **_: object) -> None:
    """Mark this worker's multiproc Gauge files as dead on shutdown.

    Called by Celery's per-prefork-child shutdown signal (not the
    once-per-worker shutdown). Each prefork child has its own pid;
    one shutdown signal fires per child.
    """
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return
    # Import inside the hook so the celery module doesn't pull the
    # prometheus_client dep at import time when the env isn't set
    # (matters for the few celery-side scripts that boot without it).
    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(pid)
