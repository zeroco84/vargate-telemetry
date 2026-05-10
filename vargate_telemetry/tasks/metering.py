# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Celery task that drains the Redis meter counters into Postgres (T2.3).

Scheduled by Celery beat once a minute; see `celery_app.py`'s
`beat_schedule`. The actual flush logic lives in
`vargate_telemetry.metering.flush`; this module is just the Celery
binding.
"""

from vargate_telemetry.celery_app import celery_app


@celery_app.task(name="vargate_telemetry.tasks.metering.flush_counters")
def flush_counters() -> int:
    """Beat-scheduled flush. Returns the number of (tenant, bucket, type) rows processed."""
    from vargate_telemetry.metering import flush

    return flush()
