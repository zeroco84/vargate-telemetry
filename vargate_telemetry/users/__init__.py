# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Cross-surface user identity stitching (TM3 Phase C).

A single human appears under disconnected identities across
Anthropic's surfaces (Code Analytics actor email, MCP chat user
email, etc.). This package reconciles those into ``user_aliases``
rows so the ``/users`` view can roll one person's activity up
across every surface.
"""

from vargate_telemetry.users.aliases import (
    ACTOR_KEY_SQL,
    SESSION_SOURCE_APIS,
    reconcile_aliases_for_tenant,
)

__all__ = [
    "ACTOR_KEY_SQL",
    "SESSION_SOURCE_APIS",
    "reconcile_aliases_for_tenant",
]
