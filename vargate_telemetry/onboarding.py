# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tenant onboarding — CLI fixture (T3.6 stub; T4 lands the real flow).

T4 wraps this behind SSO sign-in, the Anthropic-key validation call,
region selection, and the Vite onboarding screens. For now, this
module is the bare-minimum entry point a script or REPL session can
call to put a tenant in a state where the T3.5 pull task has
something to do:

  1. Provision the tenant DEK (T1.7).
  2. Seal the supplied admin API key under the well-known secret
     name from T3.3.
  3. (Caller-controlled) enqueue a backfill via
     `vargate_telemetry.tasks.pull_admin.backfill_admin_for_tenant.delay`.

Step 3 is deliberately separate so tests and CLI sessions can opt
out — the backfill is a Celery dispatch with real side effects.
"""

from __future__ import annotations

from vargate_telemetry.anthropic import ANTHROPIC_ADMIN_KEY_SECRET
from vargate_telemetry.crypto.seal import provision_tenant_dek, seal_secret


def onboard_tenant_admin_key(tenant_id: str, admin_key: str) -> None:
    """Provision DEK + seal admin key. Idempotent on repeat invocation."""
    if not tenant_id:
        raise ValueError("tenant_id required")
    if not admin_key:
        raise ValueError("admin_key required")

    provision_tenant_dek(tenant_id)
    seal_secret(
        tenant_id,
        ANTHROPIC_ADMIN_KEY_SECRET,
        admin_key.encode("utf-8"),
    )


def enqueue_admin_backfill(tenant_id: str, days: int = 90) -> str:
    """Enqueue a one-shot backfill Celery task. Returns the task id."""
    if not tenant_id:
        raise ValueError("tenant_id required")

    # Imported here so the stub doesn't pull Celery into the import
    # graph at module load — convenient when scripting against a
    # half-built environment.
    from vargate_telemetry.tasks.pull_admin import (
        backfill_admin_for_tenant,
    )

    result = backfill_admin_for_tenant.delay(tenant_id, days=days)
    return result.id
