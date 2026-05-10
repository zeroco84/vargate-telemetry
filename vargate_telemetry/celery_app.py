# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Celery application instance for Telemetry workers (T1.3)."""

import os

from celery import Celery

celery_app = Celery(
    "vargate_telemetry",
    broker=os.environ["CELERY_BROKER_URL"],          # redis://redis:6379/1
    backend=os.environ["CELERY_RESULT_BACKEND"],     # redis://redis:6379/2
    include=["vargate_telemetry.tasks"],             # populated in later sprints
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
}

# Alias so `celery -A vargate_telemetry.celery_app worker` (which looks up
# the default attribute name `app`) resolves the same instance.
app = celery_app
