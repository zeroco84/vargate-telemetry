# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Declarative models for Telemetry. Every table inherits Base + TenantOwned."""

from vargate_telemetry.models.base import Base, TenantOwned
from vargate_telemetry.models.records import TelemetryRecord
from vargate_telemetry.models.secrets import EncryptedSecret, TenantDek
from vargate_telemetry.models.usage import UsageRecord

__all__ = [
    "Base",
    "EncryptedSecret",
    "TelemetryRecord",
    "TenantDek",
    "TenantOwned",
    "UsageRecord",
]
