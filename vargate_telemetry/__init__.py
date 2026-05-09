# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Vargate Telemetry — read-only AI-usage telemetry, compliance, and analytics."""

from vargate_telemetry.celery_app import celery_app

__all__ = ["celery_app"]
