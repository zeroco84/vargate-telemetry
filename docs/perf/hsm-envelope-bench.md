# HSM Envelope Encryption — Benchmark (T1.8)

**Status:** baseline measured 2026-05-09 against the dev compose stack
on prod-1. All four decision-rule thresholds (see "Concerns and
follow-ups") pass with significant margin; no follow-up optimisations
warranted at T1 scope.
**Bench script:** [`scripts/bench_hsm_envelope.py`](../../scripts/bench_hsm_envelope.py)
**Reference implementation:** [`vargate_telemetry/crypto/seal.py`](../../vargate_telemetry/crypto/seal.py)

## Why we benchmark this

Envelope encryption is the load-bearing primitive for crypto-shredding,
per-tenant key rotation, and any future scenario where Vargate has to
prove a tenant's secrets cannot be read without the HSM. The pattern is
correct in theory; T1.8 measures whether it's *fast enough* under the
volumes the product expects.

The two questions:

1. **Can we provision a tenant in a reasonable wall-clock window?**
   Onboarding has a 60-second budget on Vargate's side
   (per the product brief); a tenant DEK provision is one step in
   onboarding and shouldn't dominate the budget.
2. **Is per-call HSM unwrap fast enough that we don't need a DEK
   cache yet?** Unwrap cost ties to the tail latency of every secret
   read (e.g., looking up an Anthropic admin key before each pull). If
   it's slow, an LRU cache of unwrapped DEKs becomes a forcing function.

## Methodology

The bench provisions N tenants in sequence (default `BENCH_TENANTS=1000`)
and per tenant performs one full round trip:

1. `provision_tenant_dek(tenant)` — `generate_dek` -> `wrap_dek` (HSM)
   -> INSERT into `tenant_deks` via `session_scope` (SET ROLE vargate_app,
   set app.tenant_id, RLS active).
2. `seal_secret(tenant, name, plaintext)` — SELECT wrapped DEK ->
   `unwrap_dek` (HSM) -> AES-GCM encrypt with AAD -> UPSERT into
   `encrypted_secrets`.
3. `unseal_secret(tenant, name)` — SELECT wrapped DEK ->
   `unwrap_dek` (HSM) -> SELECT ciphertext -> AES-GCM decrypt; the
   bench asserts the round trip equals the original plaintext.

Each operation opens its own `session_scope` (matches production code
paths) and goes through the HSM via `python-pkcs11`. **No DEK caching**
— every seal and unseal incurs an HSM round trip.

Latencies are measured per-operation in seconds via `time.perf_counter`,
reported as p50 / p95 / p99 in milliseconds. Memory peak is tracked
through `tracemalloc`. Wall-clock totals are reported per operation
type and overall.

## How to run

Inside the dev stack (postgres + minio + redis + celery-worker):

```bash
docker compose run --rm celery-worker python scripts/bench_hsm_envelope.py
# or shorter for iteration:
docker compose run --rm -e BENCH_TENANTS=100 celery-worker \
    python scripts/bench_hsm_envelope.py
```

The script prints a markdown table on stdout, ready to paste into the
"Results" section below.

To clean the rows the bench leaves behind:

```bash
docker compose exec postgres psql -U vargate -d vargate_telemetry -c "
    DELETE FROM encrypted_secrets WHERE tenant_id LIKE 'bench-tenant-%';
    DELETE FROM tenant_deks       WHERE tenant_id LIKE 'bench-tenant-%';
"
```

## Results

### T1.8 baseline (no integrity tags)

Run on 2026-05-09 against the dev compose stack on prod-1
(postgres:16-alpine + SoftHSM2 in the celery-worker image, single
process). 1,000 tenants, provision → seal → unseal per tenant, no DEK
caching.

- **Tenants:** 1000
- **Total wall-clock:** 17.71 s
- **Peak Python heap:** 1.17 MB

| Operation | Total (s) | p50 (ms) | p95 (ms) | p99 (ms) |
|-----------|-----------|----------|----------|----------|
| provision |      5.69 |     4.84 |    10.46 |    12.38 |
| seal      |      7.04 |     6.02 |    12.87 |    14.44 |
| unseal    |      4.95 |     4.20 |     9.36 |    10.49 |

### T2.0 re-run (with HMAC integrity tags)

Same conditions, after the T2.0 commit added HMAC-SHA256 compute on
write and constant-time verify on read for every wrapped DEK and every
ciphertext. Tolerance per the T2.0 spec was **unseal p95 must not
increase by more than 2 ms**.

- **Tenants:** 1000
- **Total wall-clock:** 18.45 s (Δ +0.74 s)
- **Peak Python heap:** 1.19 MB (Δ +0.02 MB)

| Operation | Total (s) | p50 (ms) | p95 (ms) | p99 (ms) | Δ p95 vs T1.8 |
|-----------|-----------|----------|----------|----------|---------------|
| provision |      5.94 |     5.00 |    10.93 |    12.87 |   +0.47 ms    |
| seal      |      7.28 |     6.24 |    13.17 |    15.40 |   +0.30 ms    |
| unseal    |      5.21 |     4.36 |     9.85 |    11.91 |   +0.49 ms    |

Unseal p95 regression (+0.49 ms) is well under the 2 ms tolerance.
The HMAC compute + constant-time verify cost about 250 µs per
operation amortized — a couple of orders of magnitude under the
HSM and SQL round-trip costs.

### What the numbers tell us

- **HSM round trips are cheap on SoftHSM2.** Each `wrap_dek` /
  `unwrap_dek` call is ~1 ms or less. The full per-operation costs
  (4-15 ms) are dominated by Postgres session setup, RLS GUC
  configuration, and SQL round trips, not by the crypto.
- **Seal is the slowest operation** because it does a DB read (look up
  wrapped DEK) plus the HSM unwrap plus a DB write (INSERT/UPDATE on
  encrypted_secrets). Unseal skips the write, so it's ~30% faster.
  Provision is fastest because the DEK is generated in memory and the
  only HSM call is `wrap_dek` (one direction).
- **Memory is essentially flat.** A 1.19 MB peak heap across 1,000
  tenants confirms session_scope and the ORM are not retaining state
  between operations — the per-operation `with` blocks close cleanly.
- **HMAC integrity tags are effectively free at this layer.** The
  KEK-derived HMAC key is module-cached after the first call; per-op
  cost is just HKDF (cached output) + one SHA-256 compute + one
  `compare_digest` — all microseconds.

## Concerns and follow-ups

The decision rules from T1.8's spec, with measured outcomes:

| Threshold | Limit | Measured | Verdict |
|---|---|---|---|
| Unseal p95 — would trigger LRU DEK cache | > 50 ms | **9.36 ms** | No cache needed at T1 scope |
| Provision p99 — onboarding budget at risk | > 250 ms | **12.38 ms** | Onboarding has 240 ms+ to spare |
| 1,000-tenant total wall-clock | > 60 s | **17.71 s** | 70% under budget |
| Peak memory for the bench | > 256 MB | **1.17 MB** | Two orders of magnitude under |

**No optimisations are required at T1 scope.** Re-run if any of:

- We move from SoftHSM2 to a real network HSM (CloudHSM, Luna, etc.).
  Network round trips will likely add 1-5 ms per HSM call; the LRU
  cache discussion may re-open.
- Per-tenant operation rate exceeds ~50 ops/sec sustained. Single-
  process, single-threaded SoftHSM2 caps somewhere around there. T1.9
  prod overlay and T2+ workers will warrant rethinking concurrency.
- The wrapped-DEK pattern changes (e.g., AES-KEY-WRAP-PAD comes back if
  python-pkcs11 fixes its broken GCM packing, or we move to a
  different PKCS#11 wrapper). Different mechanism, different latency
  shape — re-bench.

## Notes for re-running

- Bench is single-process, single-threaded by design. SoftHSM2 is not
  thread-safe in `python-pkcs11`'s wrapper, so multi-threading would
  need session-per-thread. Real concurrency story comes once we move
  past dev (T1.9 prod overlay and beyond).
- Numbers from this dev box are not directly comparable to production.
  T1.9 prod overlay will warrant a re-run.
