# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Celery task modules. Importing the package registers all task modules."""

# Import side-effect: each submodule registers its tasks with celery_app
# at import time, so the worker only needs to load `vargate_telemetry.tasks`
# (which it does via celery_app's `include=[...]`) to discover them all.
from vargate_telemetry.tasks import diagnostics  # noqa: F401
