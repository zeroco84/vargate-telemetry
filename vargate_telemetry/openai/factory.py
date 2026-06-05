# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Per-tenant OpenAI Admin client factory (TM8 Phase B).

Single entry point — ``admin_client_for_tenant(tenant_id)`` — that turns
the sealed OpenAI Admin key in ``encrypted_secrets`` into a wired
``OpenAIAdminClient``. Mirrors ``anthropic/factory.py`` exactly, reading
through the same per-tenant-DEK ``unseal_secret`` path.

Secret-name convention: every tenant's OpenAI Admin key is stored under
the literal name ``"openai_admin_key"`` (see ``OPENAI_ADMIN_KEY_SECRET``)
— a distinct name from ``"anthropic_admin_key"`` so a tenant can hold
both vendors' keys simultaneously. TM8 onboarding seals here; the pull
tasks read from here.

Error contract: ``unseal_secret`` raises ``LookupError`` when the tenant
has no DEK provisioned **or** no ``openai_admin_key`` sealed — i.e. the
tenant hasn't onboarded an OpenAI key. The pull tasks treat that as a
soft skip, not an error.
"""

from __future__ import annotations

from typing import Any

from vargate_telemetry.crypto.seal import unseal_secret
from vargate_telemetry.openai.client import OpenAIAdminClient

# Stable secret name for the tenant's OpenAI Admin API key
# (``sk-admin-…``). Distinct from ``anthropic_admin_key`` so both
# vendors' keys coexist under one tenant DEK.
OPENAI_ADMIN_KEY_SECRET = "openai_admin_key"


def admin_client_for_tenant(
    tenant_id: str,
    **client_kwargs: Any,
) -> OpenAIAdminClient:
    """Build an OpenAIAdminClient pre-loaded with the tenant's Admin key.

    ``client_kwargs`` are forwarded to ``OpenAIAdminClient.__init__`` so
    production callers can override timeouts / retry params (e.g. a
    longer timeout for the slow ``/costs`` pull) and tests can pass
    ``min_wait=0`` plus a mock transport.

    The plaintext key lives on the returned client's ``httpx.Client``
    headers; closing the client (or using it as a context manager)
    releases it along with the connection pool. Callers MUST NOT persist
    or log the returned key.

    Raises ``LookupError`` (propagated from ``unseal_secret``) when the
    tenant has no DEK provisioned or no OpenAI Admin key sealed.
    """
    if not tenant_id:
        raise ValueError("tenant_id required")

    api_key = unseal_secret(tenant_id, OPENAI_ADMIN_KEY_SECRET).decode(
        "utf-8"
    )
    return OpenAIAdminClient(api_key=api_key, **client_kwargs)
