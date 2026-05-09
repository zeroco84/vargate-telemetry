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
)

# Alias so `celery -A vargate_telemetry.celery_app worker` (which looks up
# the default attribute name `app`) resolves the same instance.
app = celery_app
