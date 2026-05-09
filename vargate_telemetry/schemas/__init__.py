# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Pydantic schemas for the Telemetry API surface (T2.1+).

Public re-exports kept tight; consumers import from this package, not
from the per-domain modules under it.
"""

from vargate_telemetry.schemas.records import (
    RecordType,
    SourceApi,
    TelemetryRecordIn,
    TelemetryRecordOut,
)

__all__ = [
    "RecordType",
    "SourceApi",
    "TelemetryRecordIn",
    "TelemetryRecordOut",
]
