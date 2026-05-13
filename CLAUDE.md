# CLAUDE.md — Vargate Telemetry (Ogma)

Durable project rules that survive across sprint work. Add a section when a rule comes out of an incident or a sprint review — not speculatively.

---

## Activation: backfill MUST land the user on a view that renders some of their data

If a customer just finished a backfill, they MUST land on a dashboard view that renders some of the data we just ingested. "Onboarded → empty state" is a product failure regardless of how accurate the empty state is.

The check at end-of-sprint for any feature that ingests data: pretend you're a brand-new tenant who just completed onboarding. Where does the app drop you? Does that view show your data? If not, the activation flow has a hole — fix the routing or build the view, don't ship the empty state.

T5.5 shipped Sessions as the only dashboard view. Personal-plan tenants got 77 Admin API records ingested + zero developer sessions (Personal plan has no Code Analytics access), so they landed on an empty Sessions page after a 60-second backfill. T5.5.5 closes the gap.

---

## Admin API usage records need their own dashboard view — Sessions is per-actor, not per-bucket

The ingest pipeline produces two distinct record shapes that both belong on the dashboard but in *different* views:

- **Admin API usage** records: bucket-grain. One row per (date, workspace, model) — daily aggregate of token usage. Every Admin API-keyed tenant gets these. This is the **Usage** view (T5.5.5).
- **Code Analytics sessions**: per-actor. One row per (date, actor). Enterprise plans with the right capability flag get these. This is the **Sessions** view (T5.5).

Sessions intentionally excludes Admin API records — they have no actor dimension, so per-developer-session aggregation isn't meaningful for them. Both record types are real and shippable; they just live in different views. Don't try to fold one into the other.

---

## Dashboard landing reflects ALL surfaces the tenant has data for

The `/dashboard` landing route is a tile grid, not a single CTA. Each tile corresponds to a view; add a tile when a new ingest stream comes online. Tiles surface a tiny stat if data exists ("23 sessions in last 7 days" / "$47.20 spent last 7 days"), else "No data yet."

Empty-state copy on any individual view points the user to OTHER views they *do* have data for — capability-aware. Example: Sessions empty state when `admin_api=true && code_analytics=false` reads "No developer sessions yet. You have N Admin API records — see [Usage]."

Don't let any view fail silently into a dead-end empty state. There's always somewhere else useful to send the tenant.

---

## Demo seed scripts take `--tenant-id` as a required flag — never seed "most recently created"

`scripts/seed_demo_*.py` MUST require an explicit `--tenant-id` argument. Never default to "the most recently created tenant" or any other heuristic. Heuristic-targeted seeds make sense in throwaway recon scripts but break the moment a smoke-test tenant gets created between the seed call and the test that reads it.

Discovered T5.5: the recon seed for Code Analytics landed in the smoke tenant (most recently created at recon time), not the tenant under test, and the test passed against zero rows of real data.

Pattern: argparse `--tenant-id` with `required=True`, validation against the `tenants` table at the top of `main()`, exit 1 if the tenant doesn't exist.
