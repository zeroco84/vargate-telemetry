# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Pydantic schemas for telemetry_records (T2.1).

Mirrors `vargate_telemetry.models.records.TelemetryRecord` for the
FastAPI surface (T3+). `TelemetryRecordIn` is the ingest-side
validator; `TelemetryRecordOut` is the read-side serializer.

The `metadata` JSONB column is exposed as a typed `dict[str, Any]` for
now. T3+ may tighten this into a discriminated union per `record_type`
once the per-API metadata shapes settle.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class RecordType(str, enum.Enum):
    """Valid `record_type` values on telemetry_records."""

    USAGE = "usage"
    PROMPT = "prompt"
    RESPONSE = "response"
    CODE_ANALYTICS = "code_analytics"


class SourceApi(str, enum.Enum):
    """Valid `source_api` values on telemetry_records."""

    ADMIN = "admin"
    COMPLIANCE = "compliance"
    CODE_ANALYTICS = "code_analytics"
    # TM1 — MCP connector telemetry. One row per `log_interaction`
    # call from the Claude-side MCP client. The DB column is varchar
    # (not enum), so adding the value here is the only wiring needed.
    MCP = "mcp"


class TelemetryRecordIn(BaseModel):
    """Input schema for ingesting a new telemetry_records row.

    Tenant scoping comes from the session, not from the payload — the
    `tenant_id` column is set by the ingest path, not by callers.
    Chain columns are computed by T2.2's chain producer, not supplied
    by callers either.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    record_type: RecordType
    source_api: SourceApi
    external_id: str = Field(min_length=1, max_length=256)
    subject_user_id: Optional[str] = Field(default=None, max_length=128)
    occurred_at: datetime
    content_ref: Optional[str] = Field(default=None, max_length=512)
    content_hash: bytes = Field(min_length=32, max_length=32)
    record_metadata: dict[str, Any] = Field(default_factory=dict)


class TelemetryRecordOut(BaseModel):
    """Output schema — full row as served by the read API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: str
    record_type: RecordType
    source_api: SourceApi
    external_id: str
    subject_user_id: Optional[str]
    occurred_at: datetime
    ingested_at: datetime
    content_ref: Optional[str]
    content_hash: bytes
    record_metadata: dict[str, Any]
    chain_seq: int
    chain_prev_hash: bytes
    chain_self_hash: bytes
