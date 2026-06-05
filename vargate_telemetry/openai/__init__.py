# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""OpenAI Admin API client for Ogma — client + types + factory (TM8).

Per layout decision A (CLAUDE.md "TM8 conventions"), this vendor package
holds ONLY the API I/O surface (client / types / factory / exceptions).
Celery pull tasks live in top-level ``tasks/pull_openai_*.py`` and rate
cards in ``pricing/openai_rates.py``.
"""

from vargate_telemetry.openai.client import OpenAIAdminClient
from vargate_telemetry.openai.exceptions import (
    InsufficientScope,
    OpenAIAPIError,
    RateLimited,
)
from vargate_telemetry.openai.factory import (
    OPENAI_ADMIN_KEY_SECRET,
    admin_client_for_tenant,
)
from vargate_telemetry.openai.types import (
    AuditLogActor,
    AuditLogEntry,
    AuditLogProject,
    CostAmount,
    CostBucket,
    CostResult,
    OrgUser,
    Project,
    ProjectApiKey,
    ProjectApiKeyOwner,
    UsageBucket,
    UsageCompletionsResult,
)

__all__ = [
    "OPENAI_ADMIN_KEY_SECRET",
    "AuditLogActor",
    "AuditLogEntry",
    "AuditLogProject",
    "CostAmount",
    "CostBucket",
    "CostResult",
    "InsufficientScope",
    "OpenAIAPIError",
    "OpenAIAdminClient",
    "OrgUser",
    "Project",
    "ProjectApiKey",
    "ProjectApiKeyOwner",
    "RateLimited",
    "UsageBucket",
    "UsageCompletionsResult",
    "admin_client_for_tenant",
]
