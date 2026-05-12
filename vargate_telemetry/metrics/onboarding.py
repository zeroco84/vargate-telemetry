# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Prometheus instruments for the onboarding flow (T4.7).

Three signals, each defined exactly once against the default
``prometheus_client.REGISTRY`` so the gateway's ``/metrics`` scrape
returns the same series consistently:

  - ``vargate_onboarding_step_seconds`` — Histogram by step label.
    Buckets cover the realistic latency surface of each step: the
    fastest (validate-key under fast Anthropic) lands sub-second; the
    slowest (start-backfill on a sluggish dispatch) can stretch to
    tens of seconds. 120s is the long tail.
  - ``vargate_onboarding_time_to_first_pull_seconds`` — Histogram.
    Wall-clock from SSO sign-in (``users.sso_sign_in_at``) to the
    first row appearing in ``telemetry_records`` for the tenant.
    Once per tenant ever — guarded via Redis SETNX so concurrent
    pull tasks don't double-observe.
  - ``vargate_onboarding_completion_total`` — Counter by outcome.
    ``completed`` is incremented when ``start-backfill`` returns 200
    (the last server-side gate of the flow). Abandonment outcomes are
    populated by a background sweep — out of scope for T4.7, slot is
    reserved.

Why a context manager for step duration: FastAPI handlers raise
``HTTPException`` on validation / policy failure, and we only want to
count durations of *successful* completions of each step. The
``track_step`` context manager observes in its ``else`` clause so an
exception path bypasses observation.

Why Redis SETNX for the first-pull guard: the alternative was a
``tenants.first_telemetry_recorded_at`` column, but the brief asks for
a single-column migration (``users.sso_sign_in_at``) and the rest of
the per-tenant counters already live in Redis (see ``metering.py``).
Adding a tenants column would also need a role-switch dance inside the
pull task; SETNX is one atomic Redis op and a no-op on every
subsequent pull for the same tenant. Worst case if Redis is wiped:
the next pull observes again — acceptable for a metrics signal.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

import redis
from prometheus_client import REGISTRY, Counter, Histogram

_log = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Instruments
# ───────────────────────────────────────────────────────────────────────────


ONBOARDING_STEP_SECONDS = Histogram(
    "vargate_onboarding_step_seconds",
    "Wall-clock duration of each onboarding step (server-side handler).",
    labelnames=("step",),
    # The four legal `step` values match the T4.x route surface; any
    # other label would silently work but flag it in code review.
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120),
)


ONBOARDING_TIME_TO_FIRST_PULL = Histogram(
    "vargate_onboarding_time_to_first_pull_seconds",
    (
        "Wall-clock from SSO sign-in to first telemetry_records row for "
        "the tenant. One observation per tenant ever."
    ),
    buckets=(30, 60, 90, 120, 300, 600),
)


ONBOARDING_COMPLETION_TOTAL = Counter(
    "vargate_onboarding_completion_total",
    "Onboarding flow outcomes, counted at the gate where the user exits.",
    labelnames=("outcome",),
)


# Pre-touch each label so they show up in /metrics scrapes even before
# the first real event lands. This avoids the "metric missing" gap
# Prometheus's `rate()` queries hit on freshly-deployed gateways.
for _outcome in (
    "completed",
    "abandoned_at_validate_key",
    "abandoned_at_region_select",
    "abandoned_at_start_backfill",
    "abandoned_at_loading",
):
    ONBOARDING_COMPLETION_TOTAL.labels(outcome=_outcome)

# Same for the step-duration histogram. Without this, Prom never sees
# `vargate_onboarding_step_seconds_bucket{step=...}` until the first
# successful step completes — leaving the Grafana panel rendering
# "No data" on freshly-deployed gateways instead of a flat-zero line.
for _step in ("sso", "validate-key", "select-region", "start-backfill"):
    ONBOARDING_STEP_SECONDS.labels(step=_step)


# ───────────────────────────────────────────────────────────────────────────
# track_step — context manager around each onboarding handler body.
# ───────────────────────────────────────────────────────────────────────────


@contextmanager
def track_step(step: str) -> Iterator[None]:
    """Observe the onboarding step histogram on success only.

    Usage::

        with track_step("validate-key"):
            ...  # the handler body
            return response  # observation fires here

    A raised exception (HTTPException, IntegrityError, anything) bypasses
    the observation — only successful step completions show up in the
    histogram, which matches the brief.
    """
    t0 = time.monotonic()
    try:
        yield
    except Exception:
        # Re-raise without observing; the failure-path metric (if we
        # ever want one) lives elsewhere.
        raise
    else:
        elapsed = time.monotonic() - t0
        ONBOARDING_STEP_SECONDS.labels(step=step).observe(elapsed)


# ───────────────────────────────────────────────────────────────────────────
# record_completion — bumps the completion counter by outcome.
# ───────────────────────────────────────────────────────────────────────────


def record_completion(outcome: str) -> None:
    """Increment the completion counter for the given outcome label.

    The legal outcome set is the union of the five labels pre-touched
    at module import time. Passing an unknown label still works (Prom
    will create the series) but indicates a bug — log loudly.
    """
    legal = {
        "completed",
        "abandoned_at_validate_key",
        "abandoned_at_region_select",
        "abandoned_at_start_backfill",
        "abandoned_at_loading",
    }
    if outcome not in legal:
        _log.warning("record_completion: unknown outcome %r", outcome)
    ONBOARDING_COMPLETION_TOTAL.labels(outcome=outcome).inc()


# ───────────────────────────────────────────────────────────────────────────
# observe_first_pull_if_first — once-per-tenant time-to-first-pull obs.
# ───────────────────────────────────────────────────────────────────────────


_FIRST_PULL_KEY_PREFIX = "vargate:metrics:onboarding:first_pull:"


def _redis_client() -> redis.Redis:
    """Build a Redis client from REDIS_URL. Kept lazy so import-time
    failures (env not set, dev shell without Redis up) don't poison the
    gateway boot.
    """
    return redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://redis:6379/0"),
        decode_responses=True,
    )


def observe_first_pull_if_first(
    tenant_id: str,
    sso_sign_in_at: datetime,
    *,
    now: Optional[datetime] = None,
    redis_client: Optional[redis.Redis] = None,
) -> bool:
    """Claim the 'first-pull observed' slot for `tenant_id` atomically.

    Returns ``True`` iff this caller won the race and an observation was
    emitted. Subsequent calls for the same tenant_id return ``False``
    and don't touch the histogram.

    The guard is a Redis ``SET <key> 1 NX`` — atomic on Redis, no race.
    If Redis is unreachable we log the error and skip the observation
    (the metric is best-effort; pull progress must never block on
    metrics infrastructure).

    `now` and `redis_client` are injectable for tests.
    """
    if not tenant_id:
        raise ValueError("tenant_id required")
    if sso_sign_in_at is None:
        # Old user row from before T4.7 migration — can't compute the
        # delta. Skip the observation rather than emit a garbage value.
        return False

    if sso_sign_in_at.tzinfo is None:
        # Defensive: treat naive datetimes as UTC. The DB column is
        # timestamptz so this shouldn't happen in practice, but a
        # synthesized test value can slip through.
        sso_sign_in_at = sso_sign_in_at.replace(tzinfo=timezone.utc)

    r = redis_client or _redis_client()
    key = _FIRST_PULL_KEY_PREFIX + tenant_id
    try:
        # NX = only set if missing. Returns True on first set, None
        # afterwards. No expiry — the slot is permanent.
        claimed = r.set(key, "1", nx=True)
    except redis.exceptions.RedisError as exc:
        _log.warning(
            "observe_first_pull_if_first: redis unavailable (%s); skipping",
            exc,
        )
        return False

    if not claimed:
        return False

    moment = now or datetime.now(timezone.utc)
    elapsed = (moment - sso_sign_in_at).total_seconds()
    if elapsed < 0:
        # Clock skew or test-fixture misuse. Don't poison the histogram.
        _log.warning(
            "observe_first_pull_if_first: negative elapsed (%.1fs) for "
            "tenant %s — skipping observation",
            elapsed,
            tenant_id,
        )
        return False

    ONBOARDING_TIME_TO_FIRST_PULL.observe(elapsed)
    return True


__all__ = [
    "REGISTRY",  # re-exported for test convenience
    "ONBOARDING_STEP_SECONDS",
    "ONBOARDING_TIME_TO_FIRST_PULL",
    "ONBOARDING_COMPLETION_TOTAL",
    "track_step",
    "record_completion",
    "observe_first_pull_if_first",
]
