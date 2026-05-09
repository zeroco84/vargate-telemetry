# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Declarative models for Telemetry. Every table inherits Base + TenantOwned."""

from vargate_telemetry.models.base import Base, TenantOwned

__all__ = ["Base", "TenantOwned"]
