# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Pydantic models for the Anthropic Admin API response shapes (T3.2).

**Status: best-guess scaffolding.** Field names and types here reflect
the public Anthropic Admin API conventions as of T3.2 authoring, but
no real cassette has been recorded against the live API yet. T3.x
will record real responses through `tests/_vcr_config.vcr_for_anthropic`
and any shape drift becomes a small one-shot edit: add a Field alias,
adjust an optional default, or extend `model_config.extra="allow"`
absorption coverage. The shape isn't load-bearing for T3.1's
transport tests — it's load-bearing for T3.5's pull task when the
normalized data flows into `telemetry_records`.

Every model sets `extra="allow"` so an unexpected field on the wire
doesn't crash parsing — the Anthropic API may add fields without a
versioned response shape change, and the conservative posture is to
absorb-and-log rather than refuse.

`UsageBreakdown.model` is the Anthropic model name (e.g.
`claude-3-5-sonnet-20250101`). Pydantic 2.x reserves the `model_`
namespace by default; we set `protected_namespaces=()` so the field
keeps its natural name from the wire.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class Member(BaseModel):
    """One organization-member user.

    Endpoint: `GET /v1/organizations/users`. Role values observed on
    the wire: `admin`, `developer`, `billing`, plus newer per-product
    roles like `claude_code_user`. We do not enumerate — `role` is a
    plain string so additions don't require a model bump.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    type: str = "user"
    email: str
    name: Optional[str] = None
    role: str
    added_at: datetime


class Workspace(BaseModel):
    """One organization workspace.

    Endpoint: `GET /v1/organizations/workspaces`. `archived_at` is
    `None` for active workspaces. `display_color` is the workspace's
    UI swatch as a `#RRGGBB` hex string; absent if never customized.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    type: str = "workspace"
    name: str
    created_at: datetime
    archived_at: Optional[datetime] = None
    display_color: Optional[str] = None


class UsageBreakdown(BaseModel):
    """Tokens used inside one (model, workspace, key, tier, context) slice.

    The Anthropic Admin usage report nests these inside each time
    bucket — one row per distinct (model, workspace_id, api_key_id,
    service_tier, context_window) combination. Optional fields are
    Optional because earlier API versions omitted them or because
    filtering can collapse some dimensions.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    model: str
    workspace_id: Optional[str] = None
    api_key_id: Optional[str] = None
    service_tier: Optional[str] = None
    context_window: Optional[str] = None
    input_tokens: int = Field(0, alias="uncached_input_tokens")
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class UsageBucket(BaseModel):
    """One time-bucketed usage row.

    Endpoint: `GET /v1/organizations/usage_report/messages`. Each row
    covers `[starting_at, ending_at)` and carries `results` — the
    per-dimension breakdowns within that window. Buckets are typically
    daily (`bucket_width=1d`) or hourly (`bucket_width=1h`); finer
    granularities may exist.
    """

    model_config = ConfigDict(extra="allow")

    starting_at: datetime
    ending_at: datetime
    results: list[UsageBreakdown] = Field(default_factory=list)
