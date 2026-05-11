# Sprint T3 — Completion notes

**Sprint dates:** 2026-05-10 to 2026-05-11
**Sprint goal (from the plan):** ingest real data from a real
Anthropic org. By end of sprint, a developer can paste an admin API
key and see usage data in Postgres within 5 minutes.
**Outcome:** **goal hit on the code side; manual smoke-test pending
a real test-org key.** All sprint-DoD items shipped; 55 tests pass +
1 skipped; the full pipeline (factory → client → typed methods →
backfill → cursor → chain → metering) is wired end-to-end. The
real-data smoke against a live Anthropic test org (`scripts/smoke_t3_real_pull.py`)
is shipped and runnable but has not yet been executed against a live
key — that's the T3 → T4 hand-off action.

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

**End-of-sprint test count:** 55 passed, 1 skipped (the live-Celery
test, still opt-in via `CELERY_TEST_LIVE=1`). Up from T2's 34+1.

Breakdown of T3-added tests (21 net new):

| Suite | Count |
|-------|-------|
| `test_anthropic_client.py` (T3.1) | 4 |
| `test_anthropic_admin.py` (T3.2)  | 4 |
| `test_anthropic_factory.py` (T3.3) | 2 |
| `test_telemetry_scheduler.py` (T3.4) | 4 |
| `test_pull_admin.py` (T3.5) | 5 |
| `test_backfill_admin.py` (T3.6) | 2 |

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

## What's flagged for T4

These are real but not blocking; T4 work picks them up where natural:

- **Real cassettes for T3.2 endpoints.** `tests/fixtures/cassettes/`
  ships empty with a README documenting the recording convention. T4
  onboarding manual testing is the natural moment to record real
  responses for `list_workspaces`, `list_members`, `list_usage`. Any
  drift from T3.2's best-guess models becomes a one-shot edit at
  that point.
- **Manual smoke run.** `scripts/smoke_t3_real_pull.py` is shipped
  and runnable; the actual run against a live Anthropic test org is
  pending a real admin key. Run it before T4 ships, not after.
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
  speculative correctness.
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

Expected: 55 passed, 1 skipped, total runtime ~14 s on the dev
stack.

Run conditions for the (still-pending) real-data smoke:

```
export ANTHROPIC_ADMIN_KEY_TEST=sk-ant-admin-xxx...
docker compose exec celery-worker \
  python scripts/smoke_t3_real_pull.py
```

Followed by `docker compose logs -f celery-beat` for 15+ minutes to
confirm `dispatch-admin-pulls` fires and picks up the new tenant.
