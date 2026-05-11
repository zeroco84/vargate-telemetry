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
"""

from vargate_telemetry.metrics.onboarding import (
    ONBOARDING_COMPLETION_TOTAL,
    ONBOARDING_STEP_SECONDS,
    ONBOARDING_TIME_TO_FIRST_PULL,
    observe_first_pull_if_first,
    record_completion,
    track_step,
)

__all__ = [
    "ONBOARDING_COMPLETION_TOTAL",
    "ONBOARDING_STEP_SECONDS",
    "ONBOARDING_TIME_TO_FIRST_PULL",
    "observe_first_pull_if_first",
    "record_completion",
    "track_step",
]
