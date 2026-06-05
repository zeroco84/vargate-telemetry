#!/usr/bin/env python3
# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""CLI to seed OpenAI cross-vendor demo activity into a tenant (TM8 Phase F).

Layers realistic OpenAI activity onto the SAME demo roster
``scripts/seed_demo.py --volume`` creates, so the cross-vendor dashboard
(the API Usage vendor filter, the Insights cards, and the Users
list+detail) renders OpenAI data alongside the existing Anthropic demo
data. Exercises the REAL pipeline — chain-bound ``append_telemetry_record``
rows for ``telemetry_records`` plus raw ``ON CONFLICT`` upserts for the
``openai_projects`` / ``openai_api_keys`` / ``openai_users`` side tables
(migration 0025) — so the seeded data prices, attributes, and verifies
exactly like production OpenAI data.

What it seeds (via :func:`vargate_telemetry.demo_seed.seed_openai_volume`):

  - ``openai_projects`` (2) + ``openai_api_keys`` (3) side tables.
  - ``openai_users``: one row per roster user (email = the roster email,
    so OpenAI activity stitches into the SAME ``users`` rows as Claude
    via the alias reconciler's email match) PLUS exactly one UNMAPPED
    identity (a service account with no email) for the Users "unmapped
    activity" panel.
  - ``openai_admin_usage`` records: per-(day, user) over ``--days``, a
    gpt-4o / gpt-4o-mini mix, full OpenAI usage metadata + the
    double-count-safe token split + ``metadata.user_email``.
  - ``openai_admin_costs`` records: authoritative per-project daily spend
    (``amount_value``) over ``--days``, gpt-4o/4o-mini line items.
  - ``openai_audit_logs`` records: a handful of recent events.

Run inside the gateway container (which has DB + HSM):

    docker compose -f docker-compose.yml -f docker-compose.prod.yml \\
        exec gateway python scripts/seed_demo_openai.py --tenant-id <TENANT_ID>

``--tenant-id`` is REQUIRED and never defaults, AND must already exist in
``tenants`` (this script seeds INTO an existing demo tenant — it does not
mint one). Idempotent: every record has a deterministic ``demo:``
external_id, so re-runs only add what's missing (it never deletes chain
records). Relative-dated to today so the nightly refresh keeps it inside
the 7d/30d dashboard windows.

The nightly cron (``seed_demo.py --volume``) already invokes this seed
via ``seed_volume`` — this script is the standalone entry point for an
OpenAI-only top-up or first provisioning.
"""

from __future__ import annotations

import argparse
import sys


def _tenant_exists(tenant_id: str) -> bool:
    """True if the tenant row exists. ``tenants`` has no RLS, so a plain
    engine connection is correct here (same access pattern the seed's
    ``ensure_tenant`` uses to INSERT the tenant row)."""
    from sqlalchemy import text as sql_text

    from vargate_telemetry.db import engine

    with engine.connect() as conn:
        return (
            conn.execute(
                sql_text(
                    "SELECT 1 FROM tenants WHERE tenant_id = :t LIMIT 1"
                ),
                {"t": tenant_id},
            ).first()
            is not None
        )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Seed OpenAI cross-vendor demo activity into a tenant."
    )
    ap.add_argument(
        "--tenant-id",
        required=True,
        help="Target tenant_id. REQUIRED — never defaults (seeding the "
        "wrong tenant pollutes a real audit chain). Must already exist "
        "in `tenants`.",
    )
    ap.add_argument(
        "--days",
        type=int,
        default=30,
        help="Days of OpenAI usage/cost history to seed (default 30).",
    )
    args = ap.parse_args()
    tenant_id = args.tenant_id.strip()
    if not tenant_id:
        print("error: --tenant-id must not be empty", file=sys.stderr)
        return 2
    if args.days < 1:
        print("error: --days must be >= 1", file=sys.stderr)
        return 2

    # Validate the tenant exists BEFORE seeding — this script seeds into
    # an existing demo tenant rather than minting one (seed_openai_volume
    # would happily ensure_tenant a typo'd id into existence otherwise).
    if not _tenant_exists(tenant_id):
        print(
            f"error: tenant {tenant_id!r} does not exist in `tenants` — "
            "refusing to seed (run scripts/seed_demo.py first to "
            "provision the demo tenant)",
            file=sys.stderr,
        )
        return 2

    from vargate_telemetry import demo_seed
    from vargate_telemetry.chain import verify_telemetry_chain
    from vargate_telemetry.db import session_scope
    from sqlalchemy import text as sql_text

    print(f"seeding OpenAI demo activity for tenant {tenant_id!r} "
          f"({args.days} days) …")
    res = demo_seed.seed_openai_volume(tenant_id, days=args.days)
    print(
        f"  side    : +{res['projects_added']} projects, "
        f"+{res['api_keys_added']} keys, +{res['users_added']} users"
    )
    print(
        f"  records : +{res['usage_added']} usage, "
        f"+{res['costs_added']} costs, +{res['audit_added']} audit"
    )

    # Summary off the live rows: distinct OpenAI actors + total billable
    # tokens (uncached + cached + output) across the seeded usage stream.
    with session_scope(tenant_id) as s:
        distinct_users = s.execute(
            sql_text(
                "SELECT COUNT(DISTINCT subject_user_id) FROM telemetry_records "
                "WHERE tenant_id = current_setting('app.tenant_id') "
                "AND source_api = :s AND subject_user_id IS NOT NULL"
            ),
            {"s": demo_seed.SOURCE_API_OPENAI_USAGE},
        ).scalar_one()
        total_tokens = s.execute(
            sql_text(
                """
                SELECT COALESCE(SUM(
                    COALESCE(((metadata->'result')->>'input_uncached_tokens')::bigint, 0)
                  + COALESCE(((metadata->'result')->>'input_cached_tokens')::bigint, 0)
                  + COALESCE(((metadata->'result')->>'output_tokens')::bigint, 0)
                ), 0)
                FROM telemetry_records
                WHERE tenant_id = current_setting('app.tenant_id')
                  AND source_api = :s
                  AND jsonb_typeof(metadata->'result') = 'object'
                """
            ),
            {"s": demo_seed.SOURCE_API_OPENAI_USAGE},
        ).scalar_one()
    print(
        f"  summary : {distinct_users} distinct OpenAI users, "
        f"{int(total_tokens):,} billable tokens"
    )

    v = verify_telemetry_chain(tenant_id)
    print(f"  chain   : valid={v.valid} records={v.record_count}")
    if not v.valid:
        print(
            "ERROR: chain did not verify after seeding", file=sys.stderr
        )
        return 1
    print("✓ OpenAI demo seed complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
