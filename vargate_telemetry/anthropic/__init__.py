# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Anthropic API client for Ogma — base + typed endpoints (T3.1+)."""

from vargate_telemetry.anthropic.client import AnthropicAdminClient
from vargate_telemetry.anthropic.exceptions import (
    AnthropicAPIError,
    InsufficientScope,
    RateLimited,
)
from vargate_telemetry.anthropic.factory import (
    ANTHROPIC_ADMIN_KEY_SECRET,
    ANTHROPIC_COMPLIANCE_KEY_SECRET,
    admin_client_for_tenant,
    compliance_client_for_tenant,
)
from vargate_telemetry.anthropic.types import (
    Activity,
    Actor,
    Chat,
    ChatMessage,
    ChatUser,
    ChatWithMessages,
    CodeAnalyticsRecord,
    Member,
    MessageArtifact,
    MessageContentBlock,
    MessageFile,
    MessageGeneratedFile,
    Organization,
    OrgUser,
    UsageBreakdown,
    UsageBucket,
    Workspace,
)

__all__ = [
    "ANTHROPIC_ADMIN_KEY_SECRET",
    "ANTHROPIC_COMPLIANCE_KEY_SECRET",
    "Activity",
    "Actor",
    "AnthropicAPIError",
    "AnthropicAdminClient",
    "Chat",
    "ChatMessage",
    "ChatUser",
    "ChatWithMessages",
    "CodeAnalyticsRecord",
    "InsufficientScope",
    "Member",
    "MessageArtifact",
    "MessageContentBlock",
    "MessageFile",
    "MessageGeneratedFile",
    "Organization",
    "OrgUser",
    "RateLimited",
    "UsageBreakdown",
    "UsageBucket",
    "Workspace",
    "admin_client_for_tenant",
    "compliance_client_for_tenant",
]
