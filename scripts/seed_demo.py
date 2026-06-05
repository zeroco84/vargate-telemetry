#!/usr/bin/env python3
# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""CLI to seed a tenant with synthetic demo data (TM6 T6.S).

Populates the Content, Sessions, and Usage dashboards on a fresh build /
for a customer walkthrough, exercising the REAL pipeline (chain-bound
records + AES-GCM content blobs). The logic lives in
``vargate_telemetry.demo_seed``; this is the thin entrypoint.

Run inside the gateway container (which has DB + MinIO + HSM):

    docker compose -f docker-compose.yml -f docker-compose.prod.yml \\
        exec gateway python scripts/seed_demo.py --tenant-id <TENANT_ID>

``--tenant-id`` is REQUIRED and never defaults — seeding the wrong tenant
pollutes a real audit chain. Idempotent: re-running only adds what's
missing (it never deletes chain records).
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed a tenant with demo data.")
    ap.add_argument(
        "--tenant-id",
        required=True,
        help="Target tenant_id. REQUIRED — never defaults (seeding the "
        "wrong tenant pollutes a real audit chain).",
    )
    ap.add_argument(
        "--volume",
        action="store_true",
        help="Also seed a realistic-volume org (~16 users, ~weeks of "
        "activity, millions of usage tokens) on top of the minimal seed.",
    )
    ap.add_argument(
        "--days",
        type=int,
        default=30,
        help="Days of history for --volume (default 30).",
    )
    args = ap.parse_args()
    tenant_id = args.tenant_id.strip()
    if not tenant_id:
        print("error: --tenant-id must not be empty", file=sys.stderr)
        return 2

    from vargate_telemetry import demo_seed
    from vargate_telemetry.chain import verify_telemetry_chain

    print(f"seeding demo data for tenant {tenant_id!r} …")
    result = demo_seed.seed_all(tenant_id)
    for surface, counts in result.items():
        print(
            f"  {surface:<8}: +{counts.get('added', 0)} added, "
            f"{counts.get('skipped', 0)} existing"
        )

    if args.volume:
        print(f"seeding volume data ({args.days} days) …")
        vol = demo_seed.seed_volume(tenant_id, days=args.days)
        print(
            f"  volume  : +{vol['users_added']} users, "
            f"+{vol['events_added']} events, +{vol['usage_added']} usage, "
            f"+{vol['content_added']} content"
        )
        # seed_volume also layers OpenAI activity (cross-vendor demo) onto
        # the same roster — report those stream counts too.
        print(
            f"  openai  : +{vol['openai_projects_added']} projects, "
            f"+{vol['openai_api_keys_added']} keys, "
            f"+{vol['openai_users_added']} users, "
            f"+{vol['openai_usage_added']} usage, "
            f"+{vol['openai_costs_added']} costs, "
            f"+{vol['openai_audit_added']} audit"
        )

    v = verify_telemetry_chain(tenant_id)
    print(f"  chain   : valid={v.valid} records={v.record_count}")
    if not v.valid:
        print("ERROR: chain did not verify after seeding", file=sys.stderr)
        return 1
    print("✓ demo seed complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
