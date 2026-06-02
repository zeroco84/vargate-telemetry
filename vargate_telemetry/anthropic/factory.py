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

# Stable secret name for the tenant's Compliance Access Key
# (``sk-ant-api01-...``, a separate key type from the admin key, sealed
# by the TM5 T5.1 onboarding flow). Distinct name so a tenant can hold
# both keys simultaneously: the admin key drives usage/activity ingest,
# the compliance key drives content capture (chats + message text).
ANTHROPIC_COMPLIANCE_KEY_SECRET = "anthropic_compliance_access_key"


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


def compliance_client_for_tenant(
    tenant_id: str,
    **client_kwargs: Any,
) -> AnthropicAdminClient:
    """Build a client pre-loaded with the tenant's Compliance Access Key.

    Mirrors ``admin_client_for_tenant`` but reads the
    ``anthropic_compliance_access_key`` secret (sealed by the TM5 T5.1
    onboarding flow). T5.2's content pull calls this once per tenant.

    The same ``AnthropicAdminClient`` class carries either key type —
    the key only determines which endpoints succeed (the Compliance
    Access Key reaches `/v1/compliance/apps/chats/*` content; an Admin
    key would 403 there).

    Raises ``LookupError`` (propagated from ``unseal_secret``) when the
    tenant has no DEK provisioned **or** no Compliance Access Key sealed
    — i.e. the tenant hasn't completed the T5.1 onboarding step. The
    content pull treats that as a soft skip, not an error.

    Callers MUST NOT persist or log the returned key.
    """
    if not tenant_id:
        raise ValueError("tenant_id required")

    api_key = unseal_secret(
        tenant_id, ANTHROPIC_COMPLIANCE_KEY_SECRET
    ).decode("utf-8")
    return AnthropicAdminClient(api_key=api_key, **client_kwargs)
