#!/usr/bin/env python3
# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Benchmark the HSM envelope-encryption pattern over N tenants (T1.8).

Provisions N tenants in sequence, then for each tenant: seals a secret,
unseals it, asserts the round-trip is correct. Records per-operation
latencies, prints a markdown summary that can be pasted into
docs/perf/hsm-envelope-bench.md.

Run inside the celery-worker container:

    docker compose run --rm celery-worker python scripts/bench_hsm_envelope.py

Override the number of tenants with BENCH_TENANTS:

    docker compose run --rm celery-worker \\
        -e BENCH_TENANTS=100 python scripts/bench_hsm_envelope.py

The bench leaves rows behind in tenant_deks and encrypted_secrets. Use
`bench-tenant-` as the namespace prefix; cleanup is optional. To clear
the rows after a run:

    docker compose exec postgres psql -U vargate -d vargate_telemetry -c "
        DELETE FROM encrypted_secrets WHERE tenant_id LIKE 'bench-tenant-%';
        DELETE FROM tenant_deks       WHERE tenant_id LIKE 'bench-tenant-%';
    "
"""

from __future__ import annotations

import os
import statistics
import sys
import time
import tracemalloc
from typing import Dict, List, Tuple


def _percentiles(samples_seconds: List[float], ps: Tuple[int, ...] = (50, 95, 99)) -> Dict[int, float]:
    """Return percentile values in milliseconds."""
    if not samples_seconds:
        return {p: 0.0 for p in ps}
    quantiles = statistics.quantiles(samples_seconds, n=100)
    return {p: round(quantiles[p - 1] * 1000, 3) for p in ps}


def run_bench(n: int) -> Dict[str, object]:
    """Run the full provision / seal / unseal cycle for `n` tenants."""
    # Imports are lazy so a failed import (e.g., HSM not initialized)
    # surfaces here rather than at module load time.
    from vargate_telemetry.crypto.seal import (
        provision_tenant_dek,
        seal_secret,
        unseal_secret,
    )

    tracemalloc.start()
    t_total_start = time.perf_counter()

    provision_times: List[float] = []
    seal_times: List[float] = []
    unseal_times: List[float] = []

    for i in range(n):
        tenant = f"bench-tenant-{i:04d}"
        secret_name = "bench-key"
        plaintext = f"sk-bench-secret-{i:04d}".encode("utf-8")

        t0 = time.perf_counter()
        provision_tenant_dek(tenant)
        provision_times.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        seal_secret(tenant, secret_name, plaintext)
        seal_times.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        result = unseal_secret(tenant, secret_name)
        unseal_times.append(time.perf_counter() - t0)

        if result != plaintext:
            raise AssertionError(f"round-trip mismatch at tenant {tenant!r}")

        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{n} tenants done...", file=sys.stderr)

    total_elapsed = time.perf_counter() - t_total_start
    _, peak_memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return {
        "n": n,
        "total_seconds": round(total_elapsed, 2),
        "provision_total_seconds": round(sum(provision_times), 2),
        "seal_total_seconds": round(sum(seal_times), 2),
        "unseal_total_seconds": round(sum(unseal_times), 2),
        "provision_pct": _percentiles(provision_times),
        "seal_pct": _percentiles(seal_times),
        "unseal_pct": _percentiles(unseal_times),
        "peak_memory_mb": round(peak_memory / 1024 / 1024, 2),
    }


def print_markdown(stats: Dict[str, object]) -> None:
    """Print a markdown summary suitable for paste into the bench doc."""
    print("")
    print("## Results")
    print("")
    print(f"- **Tenants:** {stats['n']}")
    print(f"- **Total wall-clock:** {stats['total_seconds']} s")
    print(f"- **Peak Python heap:** {stats['peak_memory_mb']} MB")
    print("")
    print("| Operation | Total (s) | p50 (ms) | p95 (ms) | p99 (ms) |")
    print("|-----------|-----------|----------|----------|----------|")
    for op_name, key_total, key_pct in [
        ("provision", "provision_total_seconds", "provision_pct"),
        ("seal",      "seal_total_seconds",      "seal_pct"),
        ("unseal",    "unseal_total_seconds",    "unseal_pct"),
    ]:
        total = stats[key_total]
        pct = stats[key_pct]
        print(
            f"| {op_name:<9} | {total:>9.2f} | "
            f"{pct[50]:>8.2f} | {pct[95]:>8.2f} | {pct[99]:>8.2f} |"
        )
    print("")


def main() -> int:
    n = int(os.environ.get("BENCH_TENANTS", "1000"))
    print(
        f"Running HSM envelope bench with {n} tenants "
        "(provision -> seal -> unseal per tenant)...",
        file=sys.stderr,
    )
    stats = run_bench(n)
    print_markdown(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
