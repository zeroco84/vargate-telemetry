# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Diagnostic Celery tasks used by infra smoke tests and operational ping checks."""

from vargate_telemetry.celery_app import celery_app


@celery_app.task(name="vargate_telemetry.diagnostics.add")
def add(a: int, b: int) -> int:
    """Trivial round-trip task — `add.delay(2, 3).get()` should return 5.

    Used by `test_telemetry_infra::test_celery_worker_responsive` to prove
    a real worker is consuming the queue, and as a one-shot health probe
    for ops (`celery -A vargate_telemetry.celery_app call vargate_telemetry.diagnostics.add --args='[2, 3]'`).
    """
    return a + b
