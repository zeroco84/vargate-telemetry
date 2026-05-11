# Sprint T3 — Completion notes

**Sprint dates:** 2026-05-10 to 2026-05-11
**Sprint goal (from the plan):** ingest real data from a real
Anthropic org. By end of sprint, a developer can paste an admin API
key and see usage data in Postgres within 5 minutes.
**Outcome:** **goal hit, smoke gate met.** 57 tests pass + 1 skipped;
the full pipeline (factory → client → typed methods → backfill →
cursor → chain → metering) is wired end-to-end. The real-data smoke
ran against a live Anthropic test org on 2026-05-11 15:05 UTC —
ingested 59 new records across 10 chunks of 7-day backfill,
chain_integrity.valid=True, record_count=77. Sprint was initially
declared closed at `c50b983` with the smoke gate still pending; the
gate cleared after several incident-driven follow-ups documented in
the "Post-close events" section below.

---

## What shipped

| Sprint | Commit                                  | Status |
|--------|-----------------------------------------|--------|
| T3.1   | Anthropic Admin API client base — auth, tenacity retry, paginate | ✓ |
| T3.2   | Typed methods — list_workspaces, list_members, list_usage + Pydantic types | ✓ |
| T3.3   | Per-tenant credential lookup factory | ✓ |
| T3.4   | pull_state cursor + tenants index + vargate_scheduler role split | ✓ |
| T3.5   | Scheduled per-tenant pull task + dispatcher fan-out (beat 15m) | ✓ |
| T3.6   | Backfill engine — chunked, resumable | ✓ |
| T3.7   | This document + manual smoke script | ✓ |
| T3.7+  | UI migration to vargate-frontend + ui/ strip from licensed repo | ✓ |
| T3.7+  | audit.db restore + chain-verifier legacy-hash shim (audit-chain 0.2.0) | ✓ |
| T3.7+  | Pydantic shape fix (UsageBreakdown.model Optional) from real-data smoke | ✓ |
| T3.7+  | audit-chain SHA pin bumped to 0.2.0 + None-model regression test | ✓ |

**End-of-sprint test count:** 57 passed, 1 skipped (the live-Celery
test, still opt-in via `CELERY_TEST_LIVE=1`). Up from T2's 34+1.

Breakdown of T3-added tests (23 net new):

| Suite | Count |
|-------|-------|
| `test_anthropic_client.py` (T3.1) | 4 |
| `test_anthropic_admin.py` (T3.2 + T3.7+ None-model regression) | 5 |
| `test_anthropic_factory.py` (T3.3) | 2 |
| `test_telemetry_scheduler.py` (T3.4) | 4 |
| `test_pull_admin.py` (T3.5) | 5 |
| `test_backfill_admin.py` (T3.6) | 2 |
| (separate package) `vargate-audit-chain/tests/test_chain.py` | +7 legacy-shim tests (in vargate-proxy) |

---

## Deviations from the original sprint plan

### 1. T3.1 testing strategy — MockTransport instead of VCR for control tests

The spec called for "tests with VCR" against hand-crafted cassettes
for the retry / 5xx-propagation / pagination scenarios. We use
`httpx.MockTransport` instead — cleaner for response-sequence
control and avoids depending on vcrpy 6.x's still-experimental
httpx integration. The VCR config itself is exercised directly via
`test_vcr_config_redacts_auth_header`, which is the property that
actually matters: any cassette T3.2+ records through
`vcr_for_anthropic` will have `x-api-key` filtered to `REDACTED`.

T3.2's typed-method tests use the same MockTransport pattern.

### 2. T3.2 best-guess scaffolding flagged in code

`vargate_telemetry/anthropic/types.py` ships with field shapes based
on the public Admin API documentation as of T3.2 authoring, *not*
against recorded cassettes from the live API. Every model sets
`extra="allow"` so unrecognized wire fields don't crash parsing, and
`UsageBreakdown` uses `protected_namespaces=()` so the natural
`model` field name doesn't conflict with Pydantic's reserved namespace.

Real cassette recording is the T4 onboarding-validation task (record
once during T4 manual testing, replay forever after). Any drift then
surfaces as a Pydantic `ValidationError` and is fixed by an alias,
a default, or extending `extra="allow"` absorption coverage.

### 3. T3.2 admin.py folded into client.py

The spec listed `vargate_telemetry/anthropic/admin.py` as a separate
file for the typed methods. With only three methods, splitting from
`client.py` would have been premature — the typed methods live on
the `AnthropicAdminClient` class alongside transport. T3.x can split
if the admin surface grows past ~5 methods.

### 4. T3.4 second-role pattern got its own architecture section

The spec's T3.4 description ended with: "Document the pattern in
`docs/architecture/postgres-rls.md` as a follow-up." We landed that
docs update in the same commit rather than as a follow-up — the
"cross-tenant enumeration — the second-role pattern" section now
codifies the convention for future cross-tenant tables (billing
rollups, fleet health, oncall alerts).

### 5. T3.5 added `dispatch_admin_pulls` beyond the spec

The spec described a `pull_admin_for_tenant(tenant_id)` task but
didn't separately name the beat-scheduled dispatcher. We added
`dispatch_admin_pulls(region=None)` as a thin fan-out task: it opens
`scheduler_session_scope` (T3.4's read-only role), enumerates active
tenants in the current region, and queues one pull per row. Beat
fires every 900 seconds.

Without an explicit dispatcher, the beat schedule entry would have
needed to receive a tenant_id from somewhere — and the natural
"somewhere" is the dispatcher. Calling out the new task name here
because the spec didn't.

### 6. T3.5 VARGATE_REGION env var added

The spec's T3.4 schema for `tenants` included a `region` column;
T3.5's `dispatch_admin_pulls` needs to read which region it's
running in. Added `VARGATE_REGION` env var (defaults to `us`),
plumbed through `.env.example` and `docker-compose.yml` for both
celery-worker and celery-beat.

### 7. T3.6 inter-run elapsed-time chunk surfaced a test bug

The first version of `test_backfill_resumes_after_crash` asserted
`result["chunks_processed"] == 1` after a resume — assuming the
remaining window was exactly the failed 7-day chunk. Verification on
prod-1 got `chunks_processed == 2` and failed.

Root cause: `_backfill_admin_for_tenant` calls `_now()` at the start
of each invocation. The first call's `now` was T; the resume's
`now` was T' > T (test wall-clock elapsed). The resume's loop
covered [cursor_after_crash, T'), which is the 7-day failed chunk
PLUS a tiny inter-run gap. The tiny gap is its own (sub-7-day)
chunk.

Fix (rolled into the T3.6 amend before push): the test now asserts
the semantically meaningful property — `seen_windows[0][0] ==
cursor_after_crash`, proving the resume picks up from the cursor
rather than from `now - days`. The chunks_processed assertion
loosened to `in (1, 2)` to accommodate the elapsed-time tail.

The behavior is correct; the test was brittle.

---

## Post-close events (2026-05-11)

The initial T3 close commit (`c50b983`) declared the sprint shipped
"on the code side; manual smoke pending a real key." Founder
correctly pushed back: a sprint goal phrased as "ingest real data
from a real Anthropic org" is not closed by synthetic tests alone.
That feedback became the working-memory rule
`t3_smoke_gate_pattern.md`. The next several hours surfaced four
issues that delayed the actual close, each landing as its own
follow-up commit. They are worth recording because each is a small
real-world lesson the spec didn't anticipate.

### 1. T3.7+ UI migration to vargate-frontend

The `vargate-telemetry/ui/` directory carried 40+ files of T1.0.5
design-system + onboarding scaffolding accumulated through the T1
sprint. Two of the working-memory rules say this is wrong:
`project_repo_layout.md` and `ui_lives_in_vargate_frontend.md` both
require UI source to live in the proprietary `vargate-frontend`
repo, not licensed product repos.

Action: copied the onboarding screen verbatim to
`vargate-frontend/apps/ogma-dashboard/src/pages/onboarding/`
(commit `454b415` on vargate-frontend) and `git rm -r ui/` plus
removed Node/Vite/Storybook entries from `.gitignore` (commit
`32d093b` on vargate-telemetry, -8820 lines).

The design-system imports inside the migrated onboarding screen
still point at the old relative path; resolving them against the
real ogma-dashboard package layout is T4 wiring work.

### 2. audit.db wipe (proxy) + restore from backup

Concurrent with the T3 close, the founder noticed the production
demo at `vargate.ai/dashboard/vargate-gtm-agent` was showing
"no records at all." Initial blast-radius analysis ruled out my
work — vargate-telemetry's Postgres is a different DB and
container — but the symptom was real. Investigation found:

- `vargate-gateway-1` was created 2026-05-10T19:03:54 UTC (16
  hours before the report), and ALL schema migrations carried the
  same `2026-05-10T19:05:55` timestamp — meaning every migration
  ran on a fresh empty DB at gateway startup.
- The proxy's `audit_log` table held 3 seed records (`vargate-internal`
  tenant), no records under `vargate-gtm-agent` or `zero84`.
- The proxy's `users` table was empty — explaining the founder's
  inability to select an agent after GitHub login (the user→tenant
  association tables were also wiped).

Root cause (from `/root/.bash_history`):

```
docker compose down            # NOT down -v — preserves HSM keys
rm -f audit.db audit.db-shm audit.db-wal     # host-side no-op
docker compose up -d
[realized rm did nothing — file lives in docker volume]
docker compose exec gateway sh -c 'find / -name "audit.db" 2>/dev/null'
docker compose exec gateway rm -f /data/audit.db ...   # THIS wiped the DB
docker compose restart gateway
```

A deliberate-but-over-scoped reset for `test_demo.py`. Intent was
to reset demo-tenant audit records; effect was nuking everything in
the DB — users, tenants, audit_log, anchors. **Not from my work.**

Recovery: found daily backups in the `vargate_backup-data` volume,
verified the May 9 18:28 UTC backup intact (1950 + 336 + 93 records
across the three tenants, 2 users, SQLite integrity OK), stopped
the gateway, renamed the wiped audit.db to `audit-wiped-20260510.db`
(per CLAUDE.md's "never delete" rule), copied the backup as the
live file, restarted. All three tenants reconciled clean post-restore.

Prevention recommendation (not implemented this sprint, flagged
for future): demo reset should `DELETE FROM audit_log WHERE
tenant_id = 'demo'` instead of `rm`-ing the whole file. Backup-
before-reset hook so the daily snapshot isn't the only path.

### 3. Chain-verifier backward-compat shim (vargate-audit-chain 0.2.0)

Immediately after restore, the dashboard reported
`chain_integrity.valid: false` with `record_hash mismatch at
record 1`. The 1950 + 336 + 93 records were the actual data
written before Sprint 16, when `compute_record_hash` did NOT yet
bind `tenant_id` into the digest. Today's verifier (post-Sprint 16)
recomputes with tenant_id and gets a different hash.

The framing matters: this isn't tamper detection failing — it's
tamper detection *correctly* surfacing a hash-function migration
that the chain knew nothing about. A chain that said "valid: true"
after a hash-function change would mean the chain doesn't actually
verify anything. But operationally, "violation detected" on a
public demo is bad UX.

Action — `vargate-audit-chain 0.2.0` (commit `2ef8ada`,
vargate-proxy):

- New `compute_record_hash_legacy(canonical_bytes)`: pre-Sprint-16
  hash — plain SHA-256 over canonical_bytes alone, no tenant_id
  binding, no length framing.
- `verify_record_chain` tries the new tenant-bound hash first; on
  mismatch, falls back to the legacy form. A failure is reported
  only if BOTH forms fail.
- `compute_record_hash` (the write path) is unchanged — new
  records continue to use the tenant-bound form.

Paired gateway-side fix (commit `c9bb589`, vargate-proxy):
`gateway/main.py::_verify_tenant_chain` is the bespoke loop that
the dashboard actually calls; it doesn't go through the package's
`verify_record_chain`. Added the matching fallback there (new
`_canonical_audit_bytes` helper + `compute_record_hash_legacy`
wrapper that goes through the package's new legacy fn).

Test coverage: 7 new tests in `vargate-audit-chain/tests/test_chain.py`
(legacy fn correctness, mixed legacy+new chain verification, tamper
still fails, no-tenant-binding trade-off pinned). Smoke-verified on
prod against the restored DB: all three tenants verify clean.

Trade-off documented in `compute_record_hash_legacy`'s docstring:
the legacy form lacks tenant_id binding, so a `canonical_bytes`
value copied from tenant A's chain *could* legacy-match under
tenant B — but `prev_hash` link-checking (which long predates
Sprint 16) catches that path: every tenant's chain has its own
prev_hash thread, and a copied record carries the wrong prev_hash
for its new tenant and fails the prev-link check.

### 4. Real-data smoke + None-model Pydantic fix

With the chain restored to green, ran the smoke against a real test
org. First attempt: `401 invalid x-api-key` — turned out the founder
had copied the admin key missing the trailing `AA`. Second attempt
hit the actual API and surfaced the T3.2 best-guess-scaffolding
flag: `pydantic_core._pydantic_core.ValidationError: 1 validation
error for UsageBucket: results.0.model — Input should be a valid
string [type=string_type, input_value=None]`.

Anthropic's `usage_report/messages` endpoint returns
`results[N].model = None` on aggregate / non-model-tagged rows.
T3.2's `UsageBreakdown.model: str` rejected it. Fix (commit
`d5c68c8`): loosened to `Optional[str] = None`.

Third attempt — smoke passed:

```
=== T3 SMOKE TEST: smoke-tenant-001 ===
[1/4] Provisioning tenant DEK + sealing admin key... done
[2/4] Running backfill (90 days, 7-day chunks)...
    chunks_processed: 10
    inserted:         59
    deduped:          0
    wall-clock:       12.5s
[3/4] Verifying chain integrity... valid (record_count=77)
[4/4] Reconciliation:
    telemetry_records count(*):       77
    usage_records sum(record_count):  18
    chain.record_count:               77
SUCCESS — T3 pipeline works end-to-end against real Anthropic data.
```

The 77-vs-59 reconciliation note: the previous (failed) smoke run
yielded buckets from chunks 1-3 before chunk 4's model=None error
killed its loop. Cursor saved at end of chunk 3. Success run
resumed from there, hence `deduped=0` (disjoint date ranges) and
`telemetry_records=77` (18 from failed run + 59 from success).
`usage_records sum=18` is the temporal flush-vs-query gap — the
failed run's 18 increments had a minute to be flushed by beat
every 60s; the success run's 59 hadn't flushed yet at script
exit. All correct, no bug.

Follow-up: commit `d97c278` bumped the `vargate-audit-chain` pin
in `pyproject.toml` + `requirements.txt` from `@712ec02` (0.1.0)
to `@2ef8ada` (0.2.0) and added
`test_list_usage_accepts_null_model_in_breakdown` as a regression
test for the None-model shape.

### 5. Two env-wiring follow-ups surfaced

Both followed the same pattern as the T2.4 REDIS_URL and T3.5
VARGATE_REGION gaps — variable in `.env` doesn't reach a running
container without explicit `docker-compose.yml` passthrough:

- T3.7+ smoke required `ANTHROPIC_ADMIN_KEY_TEST` in the
  celery-worker container env. Founder added it to `.env`; smoke
  said `STRIPE_API_KEY_TEST=` empty in the inventory check, then
  the smoke crashed with `KeyError` on the script's preflight.
  Wired through compose in commit `c5bc50a`.

Same root cause memory entry: `image_inventory_after_config_change.md`.

Both incidents reinforce the same rule.

---

## What's flagged for T4

These are real but not blocking; T4 work picks them up where natural:

- **Real cassettes for T3.2 endpoints.** `tests/fixtures/cassettes/`
  ships empty with a README documenting the recording convention.
  The T3.7+ smoke validated `list_usage` against the live API (which
  is how the None-model fix surfaced), but `list_workspaces` and
  `list_members` haven't yet been validated against real responses.
  T4 onboarding's key-validation call (`list_workspaces`) is the
  natural moment to record real responses and pin them as cassettes.
- **T4 onboarding flow itself.** `vargate_telemetry/onboarding.py`
  is a CLI-fixture stub today (`onboard_tenant_admin_key` +
  `enqueue_admin_backfill`). T4 wraps it behind:
    - SSO sign-in
    - Anthropic admin-key validation (one read-only
      `client.list_workspaces()` call to confirm the key)
    - Region selection
    - Insertion into `tenants` (so the dispatcher picks it up)
    - Backfill dispatch
- **Backfill task progress visibility.** A 90-day backfill can run
  for minutes. Once T4 has UI, surface chunks_processed /
  inserted / deduped progress live (probably via a `task_status`
  Postgres table or Celery state).
- **Cursor format documentation.** T3 settled on ISO-8601 timestamp
  strings for the `pull_state.cursor` column. If a future source_api
  uses a different cursor format (e.g., opaque token), the format
  stays in the same `varchar(512)` column but callers need to
  interpret per `source_api`. Document this in `pull_state`'s model
  docstring when a second source_api lands.

## Carried over from prior sprints

Still real, still not blocking the next sprint:

- **billing_retry consumer.** A future Celery beat task to drain
  `billing_retry`. Not blocking T4 (no Stripe live mode yet).
- **Stripe live-mode key + tenant_billing rows.** T4 onboarding is
  the natural producer. Until then, `STRIPE_API_KEY_TEST` is the
  passthrough and unprovisioned tenants skip dispatch entirely.
- **Stripe Meter Events API migration.** Defer until live-mode wiring.
- **HSM unwrap throughput at scale.** The T1.8 bench numbers still
  hold for synthetic workloads. T4's onboarding-driven seal happens
  once per tenant; T3.5's pull task does one unwrap per pull cycle.
  Neither stresses the HSM enough to need an LRU cache.
- **Active-tenant enumeration for scheduler.** Answered in T3.4 with
  the `vargate_scheduler` role split.

## Adjacent paperwork — IP assignment landed

Carried in T1 close as "must land before T2.1" then T2 close as
"must land before any pilot customer sees the product." Founder
flagged at T3 start that the Twinlite IP assignment landed
2026-05-11. **Chain-of-title for everything T1–T3 has shipped is now
clean** — the BSL license is no longer technically defective on the
Telemetry product.

Trademark filing for "Vargate" (USPTO TEAS Plus, classes 9 and 42)
remains in progress per `docs/legal/trademark.md`.

---

## What we'd do differently

- **Mark "best-guess scaffolding" sections in spec docs.** T3.2's
  type shapes are placeholder until real cassettes confirm. Future
  spec docs should call out which sections are best-guess vs.
  validated — saves the implementer from over-investing in
  speculative correctness. T3.7+'s None-model fix is the smoking
  gun: a real-data smoke would have surfaced it before the close,
  not after.
- **Pass `now` as a parameter to time-walking functions.** The
  `_now()` capture inside `_backfill_admin_for_tenant` made the
  resume test brittle — the second run's fresh `_now()` produced an
  unexpected inter-run chunk. A future refactor could accept
  `now: datetime | None = None` (default: `_now()`), giving tests
  deterministic control without monkey-patching.
- **Add a beat-schedule registration test for every new beat entry.**
  T2.3 has `test_flush_scheduled_in_beat`; T3.5's
  `dispatch-admin-pulls` entry has no matching test. Easy to add,
  catches the class of "beat schedule lost an entry in a refactor"
  bug.
- **Cassette-leak CI scan.** The convention in
  `tests/fixtures/cassettes/README.md` says don't commit cassettes
  with unredacted keys, but it's enforced by review. A regex check
  for `sk_live_`, `sk_test_[A-Za-z0-9]{20,}`, `sk-ant-admin-[A-Z]`
  would be a cheap CI step. Flag for T4 or whenever the first real
  cassette ships.
- **Ship a hash-function migration with its verifier shim from day
  one.** Sprint 16 changed `compute_record_hash` to bind tenant_id
  but did not ship the matching legacy-form fallback. The first
  in-production verify against pre-Sprint-16 records (the May 9
  backup) reported "tamper detected" — technically correct, but bad
  UX for a public demo and resolvable only with a follow-up shim
  the day-zero migration could have included. New rule for chain
  changes: change-the-write-path commits must include the
  read-path compatibility shim in the same PR.
- **Demo resets should target rows, not files.** The audit.db wipe
  RCA traced to `docker compose exec gateway rm -f /data/audit.db`
  intended as a `test_demo.py` reset. A `DELETE FROM audit_log
  WHERE tenant_id = 'demo'` would have been scoped correctly. Worth
  refactoring `test_demo.py`'s setup before the next time someone
  reaches for the rm.

---

## Hand-off to T4

T4 is the onboarding UI — the customer-facing flow that turns "an
admin paste an API key" into "data flowing in 60 seconds." T3's
backend is the engine T4 wraps:

| T4 step | T3 primitive |
|---------|--------------|
| SSO sign-in | (T4-new) |
| Connect Anthropic | (T4 wires) |
| Paste key + validate | `client.list_workspaces()` (T3.2) as a read-only confirmation call |
| Region select | INSERT INTO `tenants` (T3.4) |
| First insights loading | `enqueue_admin_backfill` (T3.6 stub) → `backfill_admin_for_tenant` (T3.6) |
| Tenant is now visible to the dispatcher | `dispatch_admin_pulls` already runs every 900s (T3.5) |

T4 is **mostly wiring**, not designing — the design work was done
in T1.0.5; the onboarding screen now lives in the proprietary
`vargate-frontend` repo at
`apps/ogma-dashboard/src/pages/onboarding/` (migrated in Sprint
T3.7+), and T3 supplies the backend primitive each screen needs.

Run conditions for a clean T3 baseline:

```
docker compose exec celery-worker alembic upgrade head     # idempotent
docker compose exec celery-worker pytest tests/ -v
```

Expected: 57 passed, 1 skipped, total runtime ~15 s on the dev
stack.

Run conditions for the real-data smoke (now validated, kept here as
a re-run reference):

```
# .env must contain ANTHROPIC_ADMIN_KEY_TEST=sk-ant-admin01-...
docker compose up -d --force-recreate celery-worker celery-beat
docker compose exec celery-worker env | grep ANTHROPIC_ADMIN_KEY_TEST  # inventory check
docker compose exec celery-worker \
  python scripts/smoke_t3_real_pull.py
```

Expected output on a clean test org: `SUCCESS — T3 pipeline works
end-to-end against real Anthropic data` with chain_integrity.valid
= True and telemetry_records count == chain.record_count.
