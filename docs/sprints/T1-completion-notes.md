# Sprint T1 — Completion notes

**Sprint dates:** 2026-05-08 to 2026-05-09
**Sprint goal (from the plan):** all new infrastructure (Postgres, MinIO,
Celery) running cleanly in dev, the HSM extended with envelope
encryption, and the Telemetry module skeleton in place. No
customer-facing functionality.
**Outcome:** **goal hit.** All sprint-definition-of-done items shipped;
14 tests pass + 1 skipped; the HSM envelope bench is well under every
threshold.

---

## What shipped

| Sprint | Commit(s) | Status |
|--------|-----------|--------|
| T1.0   | BSL-licensed bootstrap of vargate-telemetry repo | ✓ |
| T1.0.5 | Brand identity + Storybook config + build fixes | ✓ |
| T1.1   | Postgres 16 in dev compose, idempotent bootstrap | ✓ |
| T1.2   | MinIO in dev compose, distinct CHANGEME placeholders | ✓ |
| T1.3   | Celery worker + beat on Redis broker, base Dockerfile | ✓ |
| T1.4   | SQLAlchemy + Alembic + tenant-scoped session_scope, 4 infra tests | ✓ |
| T1.5   | RLS baseline + non-super app role + SET ROLE pattern, 3 RLS tests | ✓ |
| T1.6   | HSM KEK + AES-CBC-PAD wrap/unwrap, 4 crypto tests | ✓ |
| T1.7   | Per-tenant DEKs + encrypted_secrets with RLS, 4 seal tests | ✓ |
| T1.8   | HSM envelope bench, 1000-tenant baseline | ✓ |
| T1.9   | Production overlay with per-service memory limits | ✓ |
| T1.10  | This document + smoke test + sprint wrap | ✓ |

**End-of-sprint test count:** 14 passed, 1 skipped (the live-Celery
test, opt-in via `CELERY_TEST_LIVE=1`).

---

## Deviations from the original sprint plan

The plan was written assuming Telemetry would extend the existing Pro
compose stack (which has Redis, SoftHSM2, etc. already). The new
separate-repo decision (codified in ADR-001) meant Telemetry had to
bootstrap each piece itself. That was the source of most of the
sprint-time deviations.

### 1. Repo had to bootstrap services Pro already had

T1.1, T1.2, T1.3, T1.6 each spec said "files to touch:
docker-compose.yml" as if it existed. It didn't. We created
`docker-compose.yml` from scratch in T1.1 and added one service per
sub-sprint. T1.3 also implicitly added Redis because the spec assumed
"existing Redis service." T1.6 baked SoftHSM2 into the celery-worker
image and added the `vargate-hsm-tokens` named volume.

**Net effect:** every T1.x infra sprint took ~30 minutes longer than
the spec implied, because we were creating files rather than amending
them. No re-scoping needed; just slower per task.

### 2. ALTER USER NOSUPERUSER does not work on the bootstrap user

T1.5's spec said to strip SUPERUSER from the application role. Postgres
forbids removing SUPERUSER from the cluster's bootstrap user with a
fixed error message ("The bootstrap user must have the SUPERUSER
attribute"); the safety check exists so a cluster cannot end up with no
superusers.

**Workaround:** migration `0002_create_app_role` creates a separate
`vargate_app` role (NOLOGIN, NOSUPERUSER), grants the bootstrap role
membership in it, and `session_scope` issues `SET LOCAL ROLE
vargate_app` at the start of every transaction. Migrations still run as
the bootstrap superuser (so DDL is unrestricted); only the application
data path runs under the non-super role, which is what RLS needs.

This is the right pattern for production anyway — the bootstrap user
should be reserved for emergency admin, not application traffic. We
ended up with that posture as a side effect of the kernel limitation.

### 3. python-pkcs11 0.7.x has broken AES-GCM mechanism packing

T1.6's spec called for AES-GCM via the HSM's encrypt/decrypt path. The
`python-pkcs11` library raises a bare `TypeError` deep inside its
Cython binding when packing the GCM mechanism parameters. The
alternative (`AES_KEY_WRAP_PAD` via `wrap_key`/`unwrap_key`) is
unreachable because `wrap_key` doesn't exist on the `SecretKey`
instance the library returns from `get_objects` (the WrapMixin methods
don't make it onto the runtime class).

**Workaround:** AES-CBC-PAD via the standard encrypt/decrypt path.
Documented trade-off: CBC-PAD has no AEAD, so a tampered wrapped DEK
decrypts to garbage rather than raising at unwrap time. Defense in
depth comes from RLS + the seal/unseal API being the only writer to
`tenant_deks`. T1.7+ may layer HMAC if the threat model demands it.

The bench (T1.8) confirms this is fast enough on SoftHSM2 — every
threshold passes with significant margin — so we didn't pay a
performance tax for the workaround.

### 4. HSM volume was wiped three times during T1.6

The KEK's PKCS#11 capabilities (ENCRYPT|DECRYPT vs WRAP|UNWRAP) cannot
be retrofitted onto an existing key. Each iteration on the wrap
mechanism (GCM → KEY_WRAP_PAD → CBC-PAD) required regenerating the KEK,
which in PKCS#11 terms means wiping the token's data dir and
re-running `init_telemetry_kek.py`.

This was **safe** at the time because the HSM volume held only the KEK
— no tenant data. From T1.7 onward, wiping the HSM volume crypto-shreds
every wrapped DEK. The README's `down -v` warning has been updated to
reflect this.

### 5. Migration numbering shifted by one

The plan numbered migrations starting at `0001_enable_rls`,
`0002_create_encrypted_secrets`, `0003_create_telemetry_records`, etc.
We ended up with `0002_create_app_role` (added in T1.5 to fix the
superuser bypass) ahead of `0003_create_encrypted_secrets`. Future
sprint plans should be read with this +1 offset on T1.5+ migration
numbers.

---

## Bench numbers

Recorded in [`docs/perf/hsm-envelope-bench.md`](../perf/hsm-envelope-bench.md).
TL;DR for 1,000 tenants on the dev stack:

| Operation | p50 (ms) | p95 (ms) | p99 (ms) |
|-----------|----------|----------|----------|
| provision |     4.84 |    10.46 |    12.38 |
| seal      |     6.02 |    12.87 |    14.44 |
| unseal    |     4.20 |     9.36 |    10.49 |

- Total wall-clock: 17.71 s (vs. 60 s acceptance ceiling)
- Peak heap: 1.17 MB (vs. 256 MB watermark)

No optimizations needed at T1 scope. Re-run conditions documented in
the bench doc.

---

## What's flagged for T2

These are real, but not blocking; T2.1+ work picks them up:

- **DEK caching.** Bench numbers are healthy without a cache. If a
  network HSM (CloudHSM, Luna) replaces SoftHSM2, the per-call
  unwrap latency will spike and an LRU cache becomes worth building.
  Bench doc records the decision rule (unseal p95 > 50 ms).
- **AEAD on the wrapped DEK.** CBC-PAD has no integrity check.
  Threat-model risk is bounded by RLS + write-path constraints, but
  layering an HMAC over `(tenant_id, wrapped_dek)` is a small,
  contained hardening if T2.x uncovers a need.
- **Tenant enumeration.** session_scope sees only one tenant at a
  time by RLS construction, which is correct for app code but
  awkward for the Celery beat scheduler that needs to fan out per
  tenant. T3.5's task block flags "Active-tenant list comes from
  tenant_billing or similar — TBD." Likely answer: a separate
  scheduler role with read-only access to a non-RLS-scoped index.

## Adjacent paperwork still open

These were flagged in T1.0 as non-blocking and stay non-blocking:

- **Trademark filing for "Vargate"** with USPTO (TEAS Plus, classes
  9 and 42). Tracked in [`docs/legal/trademark.md`](../legal/trademark.md).
- **Founder IP assignment to Twinlite Services Limited.** Tracked in
  [`docs/legal/ip-assignment.md`](../legal/ip-assignment.md). The
  doc says it must land before T1.1 begins; it didn't, but the work
  was scaffolding-only and deferring the assignment did no
  immediate harm. **It must land before T2.1** (first real product
  code) — by then there is substantial original work that needs
  clean chain-of-title to Twinlite.

---

## What we'd do differently

- **Plan migrations with renumber-tolerance.** When a sprint plan
  hardcodes migration numbers (`0001_enable_rls`,
  `0002_create_encrypted_secrets`), an unplanned migration in the
  middle (`0002_create_app_role`) breaks the numbering. Future
  sprint plans should reference migrations by purpose ("the
  encrypted_secrets migration") not by number, and the actual files
  should number themselves sequentially.
- **Verify python-pkcs11's runtime API earlier.** Three rounds on
  the wrap mechanism would have been one round if we'd run a
  `dir(kek)` check before writing the spec'd code. A
  `tests/test_pkcs11_smoke.py` that asserts the methods we plan to
  call exist on the returned objects would catch this in CI rather
  than at integration time.
- **Lead with the bench, not the implementation.** T1.6 wrote the
  KEK lifecycle, then T1.7 wrote seal/unseal, then T1.8 measured.
  If the bench had been the first thing written (with mock
  primitives), it would have surfaced the python-pkcs11 issue
  before we'd written real code against the broken APIs.

---

## Hand-off to T2

T2's first task (T2.1 — Telemetry record schema) builds on
`vargate_telemetry/models/base.py` and the RLS / role-scoped session
pattern from T1.5. The seal/unseal API in T1.7 is what stores the
Anthropic admin keys T3.3 will consume. T2's metering service uses the
existing Redis from T1.3.

The smoke test at `tests/test_telemetry_infra_smoke.py` covers the
T1 happy-path scenario end-to-end (provision → seal → unseal →
cross-tenant invisibility). Running it cold against a fresh stack is
a fast confirmation that the T1 surface is intact before adding T2
machinery.
