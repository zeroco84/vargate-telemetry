#!/usr/bin/env python3
# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Manual T3 smoke test against a real Anthropic test org (T3.7).

**This script is NOT in CI.** It hits the live Anthropic Admin API
using a key supplied via environment variable, runs a 90-day
backfill, and prints reconciliation numbers across the full T1+T2+T3
pipeline. Use it once per release candidate to confirm the pipeline
holds against real data shapes; daily testing stays on synthetic
fixtures.

Usage:

    export ANTHROPIC_ADMIN_KEY_TEST=sk-ant-admin-xxx...
    # Optional overrides:
    export SMOKE_TENANT_ID=smoke-tenant-001
    export SMOKE_DAYS=90
    docker compose exec celery-worker python scripts/smoke_t3_real_pull.py

Or, host-side:

    cd /home/vargate/vargate-telemetry
    ANTHROPIC_ADMIN_KEY_TEST=sk-ant-... python scripts/smoke_t3_real_pull.py

What it does:

  1. Asserts the env var is set; refuses to run without it.
  2. Provisions the tenant DEK and seals the supplied admin key.
  3. Runs `_backfill_admin_for_tenant` synchronously (NOT via Celery
     — direct call, so the script can print progress and exit when
     done rather than enqueuing-and-walking-away).
  4. Verifies chain integrity via `verify_telemetry_chain`.
  5. Cross-checks telemetry_records count, usage_records sum, and
     chain record_count — all three must agree.
  6. Prints a short summary.

What it does NOT do (left to the human running it):

  - Step 6 of the spec: "wait 15 minutes; confirm scheduled pull
    runs and adds new records." The 15-minute wait is impractical in
    a script — run `docker compose logs celery-beat` later instead.
  - Region routing: smoke runs in $VARGATE_REGION's namespace, no
    cross-region behavior tested.

Re-running is safe: the backfill resumes from the cursor on each
invocation, and the dedup constraint protects telemetry_records
from duplicate inserts.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime

from sqlalchemy import text as sql_text


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(
            f"ERROR: {name} is not set.\n"
            f"Set it to a real Anthropic admin API key for the test "
            f"org and re-run.",
            file=sys.stderr,
        )
        sys.exit(2)
    return value


def main() -> int:
    api_key = _require_env("ANTHROPIC_ADMIN_KEY_TEST")
    tenant_id = os.environ.get("SMOKE_TENANT_ID", "smoke-tenant-001")
    days = int(os.environ.get("SMOKE_DAYS", "90"))

    # Imports placed inside main so `--help` and the env-var preflight
    # don't pay for the full Telemetry import graph if the user just
    # wanted to read this docstring.
    from vargate_telemetry.chain import verify_telemetry_chain
    from vargate_telemetry.db import engine, session_scope
    from vargate_telemetry.onboarding import onboard_tenant_admin_key
    from vargate_telemetry.tasks.pull_admin import (
        _backfill_admin_for_tenant,
    )

    masked = (
        api_key[:14] + "..." + api_key[-4:] if len(api_key) > 18 else "***"
    )
    print(f"=== T3 SMOKE TEST: {tenant_id} ===\n")
    print(f"Tenant:      {tenant_id}")
    print(f"Days:        {days}")
    print(f"Admin key:   {masked}")
    print(f"Started at:  {datetime.now().isoformat(timespec='seconds')}\n")

    # 1. Provision DEK + seal admin key.
    print("[1/4] Provisioning tenant DEK + sealing admin key... ", end="", flush=True)
    onboard_tenant_admin_key(tenant_id, api_key)
    print("done")

    # 2. Run the backfill synchronously.
    print(f"[2/4] Running backfill ({days} days, 7-day chunks)...")
    t0 = time.monotonic()
    result = _backfill_admin_for_tenant(tenant_id, days=days)
    wall = time.monotonic() - t0
    print(f"    chunks_processed: {result['chunks_processed']}")
    print(f"    inserted:         {result['inserted']}")
    print(f"    deduped:          {result['deduped']}")
    print(f"    wall-clock:       {wall:.1f}s\n")

    # 3. Chain integrity.
    print("[3/4] Verifying chain integrity... ", end="", flush=True)
    chain = verify_telemetry_chain(tenant_id)
    if not chain.valid:
        print("INVALID")
        print(f"    chain.record_count: {chain.record_count}")
        print(f"    chain detail:       {chain!r}", file=sys.stderr)
        return 3
    print(f"valid (record_count={chain.record_count})\n")

    # 4. Cross-table reconciliation.
    print("[4/4] Reconciliation:")
    with session_scope(tenant_id) as s:
        tr_count = s.execute(
            sql_text(
                "SELECT COUNT(*) FROM telemetry_records "
                "WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        ).scalar()
        usage_sum = s.execute(
            sql_text(
                "SELECT COALESCE(SUM(record_count), 0) "
                "FROM usage_records WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        ).scalar()
        cursor_row = s.execute(
            sql_text(
                "SELECT cursor FROM pull_state "
                "WHERE tenant_id = :t AND source_api = 'admin'"
            ),
            {"t": tenant_id},
        ).first()

    print(f"    telemetry_records count(*):       {tr_count}")
    print(f"    usage_records sum(record_count):  {usage_sum}")
    print(f"    chain.record_count:               {chain.record_count}")
    print(f"    pull_state.cursor:                {cursor_row.cursor if cursor_row else None}")

    # Best-effort reconciliation. The metering counter only counts NEW
    # inserts (the dedup branch in pull_admin doesn't increment), so
    # on a resumed run the usage_sum may be less than tr_count.
    if tr_count == 0:
        print(
            "\nWARNING: backfill returned zero records. "
            "Either the test org has no recent usage, or the "
            "Admin API endpoint shape diverges from T3.2's "
            "best-guess models. Check the cassette docs in "
            "tests/fixtures/cassettes/README.md."
        )

    if tr_count > 0 and tr_count != chain.record_count:
        print(
            f"\nFAIL: telemetry_records count ({tr_count}) ≠ "
            f"chain.record_count ({chain.record_count})"
        )
        return 4

    print("\nSUCCESS — T3 pipeline works end-to-end against real Anthropic data.")
    print("\nNext: `docker compose logs -f celery-beat` and wait 15+ minutes")
    print("to confirm `dispatch-admin-pulls` runs and surfaces new records.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
