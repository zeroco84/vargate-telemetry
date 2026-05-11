# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Anthropic API client for Ogma — base + typed endpoints (T3.1+)."""

from vargate_telemetry.anthropic.client import AnthropicAdminClient
from vargate_telemetry.anthropic.exceptions import (
    AnthropicAPIError,
    RateLimited,
)
from vargate_telemetry.anthropic.factory import (
    ANTHROPIC_ADMIN_KEY_SECRET,
    admin_client_for_tenant,
)
from vargate_telemetry.anthropic.types import (
    Member,
    UsageBreakdown,
    UsageBucket,
    Workspace,
)

__all__ = [
    "ANTHROPIC_ADMIN_KEY_SECRET",
    "AnthropicAPIError",
    "AnthropicAdminClient",
    "Member",
    "RateLimited",
    "UsageBreakdown",
    "UsageBucket",
    "Workspace",
    "admin_client_for_tenant",
]
