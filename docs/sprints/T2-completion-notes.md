# Sprint T2 — Completion notes

**Sprint dates:** 2026-05-09 to 2026-05-10
**Sprint goal (from the plan):** a Telemetry record can be created,
persisted to Postgres, hash-chained onto a per-tenant chain, counted by
the metering service, and rolled up to a Stripe usage event in test
mode. End-to-end flow with mock data.
**Outcome:** **goal hit.** All sprint-definition-of-done items shipped;
34 tests pass + 1 skipped; the 1000-record end-to-end integration test
runs in ~9 s and every accumulator (telemetry_records, usage_records,
encrypted_secrets, Stripe stub, chain integrity, billing_retry) agrees
on 1000.

---

## What shipped

| Sprint | Commit                                  | Status |
|--------|-----------------------------------------|--------|
| T2.0   | Wrapped-DEK + encrypted-secret HMAC integrity tags | ✓ |
| T2.1   | telemetry_records schema (Postgres + Pydantic) | ✓ |
| T2.2   | Ogma chain wrapper over vargate-audit-chain | ✓ |
| T2.3   | Metering — Redis counters + Postgres flush | ✓ |
| T2.4   | Stripe usage dispatch + retry queue, REDIS_URL wiring | ✓ |
| T2.5   | 1000-record end-to-end integration test | ✓ |
| T2.6   | This document — T2 close | ✓ |

**End-of-sprint test count:** 34 passed, 1 skipped (the live-Celery
test, still opt-in via `CELERY_TEST_LIVE=1`).

Breakdown:

| Suite | Count |
|-------|-------|
| `test_telemetry_billing.py` (T2.4) | 3 |
| `test_telemetry_chain.py` (T2.2)   | 4 |
| `test_telemetry_crypto.py` (T1.6 + T2.0) | 8 |
| `test_telemetry_infra.py` (T1.4)   | 3 + 1 skipped |
| `test_telemetry_infra_smoke.py` (T1.10) | 1 |
| `test_telemetry_metering.py` (T2.3) | 4 |
| `test_telemetry_records.py` (T2.1) | 3 |
| `test_telemetry_rls.py` (T1.5)     | 3 |
| `test_telemetry_seal.py` (T1.7)    | 4 |
| `integration/test_t2_end_to_end.py` (T2.5) | 1 |

---

## Deviations from the original sprint plan

### 1. T2.0 (AEAD/HMAC hardening) was inserted before T2.1

The published plan opened T2 with T2.1 (record schema). After the T1
retro, T1.6's AES-CBC-PAD fallback (the python-pkcs11 GCM bug — see T1
notes) was promoted from a "T2.x maybe" to a hard T2 prerequisite:
once real Anthropic admin keys land in `encrypted_secrets` (T3.3),
retrofitting integrity is a migration with a key-derivation step over
existing tenant data. Cheap now, painful later.

**Decision:** HMAC-SHA256 over `tenant_id || ":" || ciphertext` with
the HMAC key derived from the HSM KEK via AES-CBC-PAD-encrypt(label) →
HKDF-SHA256. The KEK stays the single root of trust; crypto-shredding
it revokes integrity verification for free.

### 2. "Single chain per tenant" became "two parallel chains, shared primitives"

The T2.2 spec called for Telemetry records to extend Tyr's existing
`audit_log` chain. Reading Tyr's chain code revealed the chain-append
function is intrinsically shaped around `audit_log`'s columns —
forcing Telemetry records into the same chain would require either
schema bloat (carrying audit_log fields on telemetry_records) or a
discriminator-blob hack.

**Decision:** two chains per tenant, sharing the primitives. Each
chain is a separate sequence with its own GENESIS sentinel per tenant
per product:

- Tyr's `audit_log` chain — sentinel `SHA-256("GENESIS")`
- Ogma's `telemetry_records` chain — sentinel `SHA-256("vargate.telemetry/chain/genesis")`

Auditor-friendly (no record-type discriminators), and what the code
naturally supports. Sprint 16 (in the proxy repo) extracted
`vargate-audit-chain` (Apache 2.0) so both products consume the same
primitives. T2.2's `test_existing_pro_records_unaffected` got replaced
with `test_chains_are_isolated_per_tenant` to match the new model.

### 3. Migration revision_id length

T2.0's first attempt used `0004_add_integrity_tag_to_secrets` (33
chars). The `alembic_version` table has a `version_num VARCHAR(32)`
constraint and the upgrade raised `StringDataRightTruncation`.
Shortened to `0004_add_integrity_tag` (22 chars). Convention going
forward: revision id ≤ 32 chars; sprint specs reference migrations by
purpose, not by full identifier.

### 4. Cross-repo SHA pinning

T2.2's first commit pinned `git+ssh://git@github.com/zeroco84/vargate-proxy.git`,
but the proxy repo is `zeroco84/vargate.ai`. The push surfaced the
typo. A fix-up commit corrected pyproject.toml + requirements.txt +
the audit-chain README's install snippet. Lesson: grep
`github.com/zeroco84/` in any new Git+SSH dep URL before commit.

### 5. Sprint 16 force-push to update naming convention

The first Sprint 16 commit used "Pro" / "Telemetry" terminology in the
commit body. Mid-T2.2 the founder solidified the product names —
**Tyr** for the proxy, **Ogma** for telemetry — and asked to amend the
already-pushed Sprint 16 commit to match. `git push --force-with-lease`
applied cleanly, but the cascade required bumping the SHA in Telemetry's
dep pin a second time.

**Memory refinement (durable):** after this incident, the founder
added `feedback_push_after_commit.md`: *commit eagerly, push
deliberately*. Push is publication, distinct from verification. Amending
a pre-push commit is cheap; force-push after-push is a process
incident. Every subsequent T2 sprint (T2.3 onward) followed the
commit-then-hand-off-for-verification rhythm.

### 6. REDIS_URL wiring gap from T2.3 surfaced during T2.4

`metering.py` read `os.environ["REDIS_URL"]` since T2.3, but the
celery-worker / celery-beat compose env blocks didn't pass it through.
The T2.3 verification ran against a stale image that didn't have the
new metering tests bundled, so the gap stayed hidden until T2.4
verification produced six `KeyError: 'REDIS_URL'` errors at fixture
setup. Fix rolled into T2.4 (REDIS_URL + STRIPE_API_KEY_TEST added to
both celery service env blocks).

### 7. pyproject.toml was not COPY'd into the image

T2.5's `pythonpath = ["tests"]` needed pyproject.toml at /app for
pytest to honor the config. The Dockerfile copied `requirements.txt`,
`alembic.ini`, `vargate_telemetry/`, `tests/`, `scripts/` — but never
pyproject.toml. Tests had been silently running with pytest's default
config (rootdir-only) since T1. Fix rolled into T2.5: one new `COPY
pyproject.toml ./` after the pip-install layer. Verification confirmed
`configfile: pyproject.toml` in the pytest header.

---

## What's flagged for T3

These are real but not blocking; T3.x work picks them up where natural:

- **billing_retry consumer.** T2.4 writes failed Stripe dispatches to
  `billing_retry` but does not drain them. A future Celery beat task
  should retry with exponential backoff and bound `attempts`. Not
  blocking T3 — none of T3's work touches Stripe — but should land
  before any live-mode Stripe wiring.
- **Stripe live-mode key.** T2.4 uses test mode only. T4 (onboarding)
  is the natural producer of `tenant_billing` rows and the right place
  to add the live key plumbing. Until then `STRIPE_API_KEY_TEST` is
  passed through with `:-` default, and unprovisioned tenants skip
  dispatch entirely.
- **Stripe API drift — Meter Events vs. legacy.**
  `SubscriptionItem.create_usage_record` is Stripe's legacy
  usage-based-billing API. Newer integrations use `billing.MeterEvent`.
  Either works for test mode; pick when promoting to live.
- **HSM unwrap throughput at scale.** The T2.5 1000-record test ran
  in ~9 s; the dominant cost is 1000 HSM unwraps for the per-record
  seal. Fine for tests. If T3.5's production pull task does heavy
  per-record seal/unseal, an LRU cache becomes worth building (the
  decision rule from T1's bench doc stands: `unseal p95 > 50 ms`).
- **Active-tenant enumeration for the scheduler.** Carried over from
  T1 close — answered in the T3.4 spec (`vargate_scheduler` role with
  read-only access to a non-RLS `tenants` index). Not yet
  implemented; T3.4 lands it.

## Adjacent paperwork still open

Carried over from T1 close:

- **Trademark filing for "Vargate"** with USPTO (TEAS Plus, classes
  9 and 42). Tracked in `docs/legal/trademark.md`.
- **Founder IP assignment to Twinlite Services Limited.** Tracked in
  `docs/legal/ip-assignment.md`. T1's note said it must land before
  T2.1; it didn't. T2 has shipped substantial original work (chain
  wrapper, metering, billing dispatch) — the chain-of-title gap is now
  concrete rather than theoretical. **It must land before any pilot
  customer sees the product.**

---

## What we'd do differently

- **Inventory the image at every config-file change.** Both
  REDIS_URL and pyproject.toml problems were "host has the right
  thing; container does not." Adding a one-line
  `docker compose exec celery-worker ls /app/` to the verification
  checklist would have caught both in seconds rather than minutes.
- **Force fresh container recreation during verification.** Multiple
  T2 verification runs landed against a `Running` container that was
  the same image as a previous run, hiding bugs. The verification
  script should always include `build celery-worker celery-beat` and
  `up -d --force-recreate`, with the explicit goal of catching
  rebuild-required gaps.
- **Verify cross-repo dep URLs at install time.** The
  `vargate-proxy.git` vs `vargate.ai` typo cost an amend + force-push.
  Worth a 5-minute CI check that `pip install -r requirements.txt`
  resolves successfully against the pinned SHA before merge.
- **Make the test image self-checking on env vars.** A small fixture
  that asserts every required env var is set at session start would
  have caught REDIS_URL the moment T2.3's tests were collected. Cheap;
  saves an amend cycle.

---

## Hand-off to T3

T3.1 (Anthropic API client base) is fresh code with no T2 dependency
— pure HTTP layer with retry, rate-limit handling, pagination,
VCR-recorded tests.

T3.3 (per-tenant credential lookup) calls T1.7's `unseal_secret(tenant_id,
"anthropic_admin_key")` and feeds the plaintext into T3.1's client
constructor.

T3.5 (scheduled pull task) is where T2's primitives compose with T3's
new work — per-tenant Celery task that:
1. Opens `session_scope(tenant_id)` (T1.5 / T1.7 RLS pattern)
2. Loads cursor (T3.4 `pull_state`)
3. Fans out HTTP calls via T3.3's client factory
4. For each row: `append_telemetry_record(...)` (T2.2) +
   `increment(tenant_id, record_type)` (T2.3)
5. Advances cursor and commits

T3.7's real-data smoke test follows the shape of T2.5's integration
test, swapping the synthetic factory for an actual Anthropic test org
and dropping the per-record `seal_secret` step (since the records
contain no encryption-worthy content payload — just metadata).

Run conditions for a clean T2 baseline:

```
docker compose exec celery-worker alembic upgrade head     # idempotent
docker compose exec celery-worker pytest tests/ -v
```

Expected: 34 passed, 1 skipped, total runtime ~13 s on the dev stack
(integration test absorbs ~9 s of that).
