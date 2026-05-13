#!/usr/bin/env python3
# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Re-pull Admin API usage with T5.5.6's group_by breakdown.

Run this once per tenant after T5.5.6 ships to populate per-(date,
model, workspace) rows alongside the existing pre-T5.5.6 aggregate
rows. The old aggregate records are NOT deleted — they coexist
because the new per-breakdown rows have a different
`(tenant_id, source_api, external_id)` tuple, so the dedup UNIQUE
doesn't conflict. The Usage view returns both shapes; the new ones
carry computable cost, the old ones render with
`estimated_cost_usd: null`.

USAGE
=====
    docker compose -f docker-compose.yml -f docker-compose.prod.yml \\
      exec gateway python -m scripts.backfill_admin_usage_breakdown \\
        --tenant-id tnt_eu_xxxx --days 90

The script never auto-targets a tenant — `--tenant-id` is required.
That's the rule from CLAUDE.md (T5.5.5): heuristic-targeted seeds
break the moment a smoke-test tenant gets created between the call
and the test that reads it.

WHAT IT DOES
============
1. Validates the tenant exists in the `tenants` table.
2. Refreshes `workspaces` rows for the tenant from the Admin API.
3. Runs the standard `_backfill_admin_for_tenant(tenant_id,
   days=days)` flow — which now passes `group_by=[model,
   workspace_id]` on every Anthropic call and emits per-breakdown
   `telemetry_records` rows.
4. Prints counts on completion.

The script is idempotent: running it twice writes nothing new on
the second run (every per-breakdown row dedups on its granular
external_id).
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text as sql_text


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--tenant-id",
        required=True,
        help="Tenant ID to backfill. Required; never defaulted.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="History window in days. Default: 90.",
    )
    args = parser.parse_args()

    # Imports go inside main() so --help doesn't require the
    # backend dependencies to be importable.
    from vargate_telemetry.db import engine
    from vargate_telemetry.tasks.pull_admin import _backfill_admin_for_tenant

    # 1. Validate the tenant exists.
    with engine.begin() as conn:
        row = conn.execute(
            sql_text(
                "SELECT tenant_id, region, billing_status "
                "FROM tenants WHERE tenant_id = :t"
            ),
            {"t": args.tenant_id},
        ).first()
    if row is None:
        print(
            f"ERROR: tenant '{args.tenant_id}' does not exist. "
            f"Run `SELECT tenant_id FROM tenants` to find the right one.",
            file=sys.stderr,
        )
        return 1
    print(
        f"Target tenant: {row.tenant_id} "
        f"(region={row.region}, billing_status={row.billing_status})"
    )

    # 2. Run the backfill. This already includes the workspace sync
    # via _sync_workspaces called at the start.
    print(
        f"Running backfill — {args.days} days, group_by=[model, workspace_id]..."
    )
    result = _backfill_admin_for_tenant(
        args.tenant_id, days=args.days
    )
    print(
        f"Done: inserted={result['inserted']}, "
        f"deduped={result['deduped']}, "
        f"chunks_processed={result['chunks_processed']}"
    )

    # 3. Surface counts of breakdown vs aggregate rows so the
    # operator can verify the breakdown landed.
    with engine.begin() as conn:
        breakdown_count = conn.execute(
            sql_text(
                """
                SELECT COUNT(*) FROM telemetry_records
                WHERE tenant_id = :t
                  AND record_type = 'usage'
                  AND source_api = 'admin'
                  AND metadata->'results'->0->>'model' IS NOT NULL
                """
            ),
            {"t": args.tenant_id},
        ).scalar()
        aggregate_count = conn.execute(
            sql_text(
                """
                SELECT COUNT(*) FROM telemetry_records
                WHERE tenant_id = :t
                  AND record_type = 'usage'
                  AND source_api = 'admin'
                  AND (
                      metadata->'results'->0->>'model' IS NULL
                      OR jsonb_array_length(metadata->'results') = 0
                  )
                """
            ),
            {"t": args.tenant_id},
        ).scalar()
        workspace_count = conn.execute(
            sql_text(
                "SELECT COUNT(*) FROM workspaces WHERE tenant_id = :t"
            ),
            {"t": args.tenant_id},
        ).scalar()
    print(
        f"After backfill: {breakdown_count} per-model rows, "
        f"{aggregate_count} legacy aggregate rows, "
        f"{workspace_count} workspaces resolved."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
