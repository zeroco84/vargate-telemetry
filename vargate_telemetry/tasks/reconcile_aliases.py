# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""User-alias reconciliation Celery task (TM3 Phase C1).

Steady-state half of the alias auto-match. Runs every 15 minutes
(same cadence as the ingest streams + the budget evaluator) so a
newly-ingested actor identifier gets an alias row — and an
auto-match if its email maps to an Ogma user — within one tick.

The ``/api/users`` endpoint ALSO reconciles lazily on read so a
freshly-onboarded tenant doesn't have to wait for a beat tick to
see stitched users (activation-readiness, see
``users/aliases.py``). Both call the same idempotent helper.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text as sql_text

from vargate_telemetry.celery_app import celery_app
from vargate_telemetry.db import scheduler_session_scope, session_scope
from vargate_telemetry.users import reconcile_aliases_for_tenant

_log = logging.getLogger(__name__)


@celery_app.task(
    name=(
        "vargate_telemetry.tasks.reconcile_aliases."
        "reconcile_aliases_for_tenant_task"
    ),
)
def reconcile_aliases_for_tenant_task(tenant_id: str) -> dict:
    """Reconcile one tenant's aliases. Opens an RLS-bound session."""
    with session_scope(tenant_id) as s:
        return reconcile_aliases_for_tenant(s, tenant_id)


@celery_app.task(
    name=(
        "vargate_telemetry.tasks.reconcile_aliases."
        "dispatch_reconcile_aliases"
    ),
)
def dispatch_reconcile_aliases(region: Optional[str] = None) -> int:
    """Beat fan-out. Enumerate active tenants; queue one reconcile each.

    Mirrors ``dispatch_admin_pulls`` / ``dispatch_evaluate_budgets``
    — same role, same query shape.
    """
    # TM5 T5.0: default dispatches all active tenants; the region gap (defaulting to VARGATE_REGION=us) silently skipped eu tenants. region arg kept as an explicit override.
    with scheduler_session_scope() as s:
        if region is None:
            rows = s.execute(
                sql_text("SELECT tenant_id FROM tenants WHERE active = true")
            ).all()
        else:
            rows = s.execute(
                sql_text(
                    "SELECT tenant_id FROM tenants "
                    "WHERE active = true AND region = :r"
                ),
                {"r": region},
            ).all()

    for row in rows:
        reconcile_aliases_for_tenant_task.delay(row.tenant_id)

    _log.info(
        "dispatch_reconcile_aliases: queued %d tenants in region %s",
        len(rows),
        region or "all",
    )
    return len(rows)
