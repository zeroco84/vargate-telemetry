# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""FastAPI gateway for Ogma — the HTTP surface defined by openapi/ogma-api.yaml."""

from vargate_telemetry.api.app import app

__all__ = ["app"]
