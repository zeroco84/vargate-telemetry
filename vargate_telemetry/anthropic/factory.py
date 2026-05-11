# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Per-tenant Anthropic Admin client factory (T3.3).

Single entry point — `admin_client_for_tenant(tenant_id)` — that
turns the sealed admin API key in `encrypted_secrets` into a wired
`AnthropicAdminClient`. T3.5's scheduled pull task calls this once
per tenant to obtain a client without ever holding the plaintext key
beyond the client's lifetime.

Secret-name convention: every tenant's admin key is stored under the
literal name `"anthropic_admin_key"` (see `ANTHROPIC_ADMIN_KEY_SECRET`).
T4 onboarding seals here; T3.3 reads from here. Other Anthropic
credentials (e.g. workspace keys, future per-tenant tokens) get their
own well-known names defined alongside this constant.

Error contract — both raise `LookupError` with a descriptive message:
  - tenant has no DEK provisioned (T1.7 pre-condition unmet)
  - tenant has a DEK but no sealed admin key
"""

from __future__ import annotations

from typing import Any

from vargate_telemetry.anthropic.client import AnthropicAdminClient
from vargate_telemetry.crypto.seal import unseal_secret

# Stable secret name for the tenant's admin API key.
ANTHROPIC_ADMIN_KEY_SECRET = "anthropic_admin_key"


def admin_client_for_tenant(
    tenant_id: str,
    **client_kwargs: Any,
) -> AnthropicAdminClient:
    """Build an AnthropicAdminClient pre-loaded with the tenant's admin key.

    `client_kwargs` are forwarded to `AnthropicAdminClient.__init__`
    so production callers can override timeouts / retry params and
    tests can pass `min_wait=0` plus a mock transport without going
    through the env-backed Stripe-style dispatcher pattern.

    The plaintext key lives on the returned client's httpx.Client
    headers; closing the client (or using it as a context manager)
    releases it along with the connection pool. Callers MUST NOT
    persist or log the returned key.
    """
    if not tenant_id:
        raise ValueError("tenant_id required")

    api_key = unseal_secret(tenant_id, ANTHROPIC_ADMIN_KEY_SECRET).decode(
        "utf-8"
    )
    return AnthropicAdminClient(api_key=api_key, **client_kwargs)
