# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Vargate Telemetry — read-only AI-usage telemetry, compliance, and analytics.

This package's `__init__.py` is intentionally empty of side-effecting
imports. T4.2 added the FastAPI gateway, which doesn't need Celery
loaded; importing `vargate_telemetry.celery_app` at package init
required `CELERY_BROKER_URL` in env, which broke the gateway
container's startup. Sub-modules are imported directly by whoever
needs them:

  - `celery -A vargate_telemetry.celery_app worker` (workers)
  - `uvicorn vargate_telemetry.api.app:app` (gateway)
"""
