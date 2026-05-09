# HSM Envelope Encryption — Benchmark (T1.8)

**Status:** baseline; numbers TBD pending first run on the dev compose
stack.
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

> **TBD** — paste output of `bench_hsm_envelope.py` here on first run.
> The script prints a markdown table directly suitable for this section.

## Concerns and follow-ups

To be filled in once numbers exist. Decision rules:

- **Unseal p95 > 50 ms.** HSM unwrap dominates every secret read; add an
  LRU cache of unwrapped DEKs keyed by `(tenant_id, kek_label)`. Eviction
  on rotation, bounded size, time-bounded TTL.
- **Provision p99 > 250 ms.** Onboarding budget is at risk; consider
  pre-provisioning DEKs from a pool, or moving provision into a Celery
  task that runs ahead of the user's first interaction.
- **Total wall-clock for 1,000 tenants > 60 s.** Aligns with the
  acceptance signal in T1.7's spec; if exceeded, the LRU cache buys
  the most.
- **Peak memory > 256 MB for 1,000-tenant run.** Probably means we're
  retaining sessions, ORM objects, or DEK material we shouldn't be.
  Profile with `tracemalloc.snapshot()`.

## Notes for re-running

- Bench is single-process, single-threaded by design. SoftHSM2 is not
  thread-safe in `python-pkcs11`'s wrapper, so multi-threading would
  need session-per-thread. Real concurrency story comes once we move
  past dev (T1.9 prod overlay and beyond).
- Numbers from this dev box are not directly comparable to production.
  T1.9 prod overlay will warrant a re-run.
