# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Prometheus instrumentation surface for the Ogma gateway.

Sub-modules:

  - ``onboarding`` (T4.7) — step durations, time-to-first-pull,
    completion outcomes. Histograms + Counter, scraped at
    ``/metrics`` on the gateway.

Anything new (billing churn, anomaly fire rate, dashboard latency) can
land alongside as separate modules; the package keeps the import paths
sane (``from vargate_telemetry.metrics.onboarding import ...``) without
giant catch-all files.

T4.8.1 — multi-process registry
================================

The gateway, celery-worker, and celery-beat are three separate Python
processes. ``prometheus_client``'s default ``REGISTRY`` is in-process,
so an observation emitted from the worker's backfill task lands in the
worker's REGISTRY only — invisible to any scrape of the gateway's
``/metrics`` endpoint. T4.8 surfaced this as a real bug: the
``time_to_first_pull_seconds`` histogram is observed inside the worker
but the scrape target is the gateway.

The fix is ``prometheus_client``'s documented multi-process mode:

  - All three services have ``PROMETHEUS_MULTIPROC_DIR`` set to a path
    on a shared tmpfs-backed volume (``vargate-prom-multiproc`` in
    docker-compose.yml). The client library writes its mmap'd Counter
    and Histogram values to files in that dir on every observation.
  - The ``/metrics`` route on the gateway builds a fresh
    ``CollectorRegistry`` and attaches a ``MultiProcessCollector`` to
    it. ``generate_latest(that_registry)`` aggregates the files across
    all processes that wrote to the dir.

``get_registry()`` below is the bridge. The metric *declarations* in
``onboarding.py`` (and any future submodule) stay unchanged — when the
env var is set, ``prometheus_client`` automatically routes their writes
to the multiproc dir; when it isn't, they fall back to the default
REGISTRY (the dev / unit-test path).
"""

from __future__ import annotations

import os

from prometheus_client import REGISTRY, CollectorRegistry, multiprocess

from vargate_telemetry.metrics.onboarding import (
    ONBOARDING_COMPLETION_TOTAL,
    ONBOARDING_STEP_SECONDS,
    ONBOARDING_TIME_TO_FIRST_PULL,
    observe_first_pull_if_first,
    record_completion,
    track_step,
)


def get_registry() -> CollectorRegistry:
    """Return the right registry for the current process's deployment.

    - With ``PROMETHEUS_MULTIPROC_DIR`` set (compose / prod / any
      multi-process deployment), build a fresh ``CollectorRegistry``
      and attach a ``MultiProcessCollector``. Each ``/metrics`` request
      gets a fresh registry; the collector re-reads the multiproc dir
      every time, so a brand-new observation in another process shows
      up on the next scrape with no caching gotchas.
    - Without the env var (dev shell, unit tests that don't fork),
      return the default in-process ``REGISTRY``.

    The route handler chooses which to call — see
    ``vargate_telemetry.api.app:_metrics``.
    """
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return registry
    return REGISTRY


__all__ = [
    "ONBOARDING_COMPLETION_TOTAL",
    "ONBOARDING_STEP_SECONDS",
    "ONBOARDING_TIME_TO_FIRST_PULL",
    "get_registry",
    "observe_first_pull_if_first",
    "record_completion",
    "track_step",
]
