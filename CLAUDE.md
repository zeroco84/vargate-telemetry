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

---

## Anthropic Admin API requires explicit `group_by` for breakdown rows

The `/v1/organizations/usage_report/messages` endpoint defaults to **aggregate-per-day** rows — every row has `model=null` and `workspace_id=null`. Per-dimension breakdown is opt-in via repeated `group_by[]` params:

```
?group_by[]=model&group_by[]=workspace_id
```

Without `group_by`, the response is forward-compatible-but-useless: the shape includes all the fields a future caller might want, populated with `null`. Pass `group_by=["model", "workspace_id"]` on every backfill / poll call so cost computation has something to multiply rates against.

T5.5.5 shipped without `group_by` and produced 77 rows of `model=null` for the founder's tenant — the Usage view rendered but the Est. Cost column was unrepresentable. T5.5.6 closes that.

`external_id` format for the multi-row case becomes `usage:{starting_at}:{ending_at}:{model_or_-}:{workspace_id_or_-}` so per-bucket dedup still works when one daily bucket produces N breakdown rows.

---

## Pricing computation needs a versioned rate card, not a flat dict

Per-model rates live in `vargate_telemetry/pricing/anthropic_rates.py` as a **versioned** structure (a `RATE_HISTORY` list of `(effective_from, effective_to, rates)` entries, plus a `CURRENT_RATES` shortcut). Rates change — Anthropic has bumped Opus pricing twice already in 2025 — and historical records must compute against the rate that was active **when `occurred_at` happened**, not against today's rate.

Helper signature:
```python
def compute_cost_usd(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    occurred_at: datetime,
) -> Decimal | None
```

Returns `Decimal` (not `float`) because this is financial-adjacent data and float drift on rate × token-count can move totals by cents over a billing cycle.

Returns `None` when the model is `null` (legacy aggregate rows) or unknown — **never fake a number**. The UI renders `—` for `None`. Faking would tell the customer a wrong dollar figure; surfacing `None` makes the gap visible and steerable.

---

## Connector-shape upgrades need a view-layer reconciliation, not a data wipe

When a connector upgrade changes the shape of records it ingests (e.g., T5.5.6 added `group_by=[model, workspace_id]` and split one daily bucket into N per-model rows), the OLD pre-upgrade records stay in the database — the audit-chain principle is **never modify chain rows**. Both shapes coexist after the cutover.

The dashboard view must reconcile them. Without reconciliation, totals double-count: the legacy aggregate row for day D carries the same tokens as the sum of the new per-model rows for day D. Customers see inflated spend and confusing duplicate rows.

The fix is **filter at the API view, not the data layer**. Whenever a per-model row exists for `(tenant, date)`, hide the legacy aggregate rows for that same `(tenant, date)`. Days that have ONLY legacy data (pre-backfill state, or genuinely zero activity captured as an empty bucket) keep their legacy row so the view doesn't go blank.

Pattern, in SQL:

```sql
AND NOT (
    (result->>'model') IS NULL
    AND EXISTS (
        SELECT 1 FROM telemetry_records tr2,
             jsonb_array_elements(tr2.metadata->'results') AS r2(result)
        WHERE tr2.tenant_id = current_setting('app.tenant_id')
          AND DATE(tr2.occurred_at AT TIME ZONE 'UTC') = DATE(tr.occurred_at AT TIME ZONE 'UTC')
          AND (r2.result->>'model') IS NOT NULL
    )
)
```

Apply to **every** aggregating query on the same surface (page rows, totals, cost-by-model) — missing one shows up as inconsistent counters between the table and the totals row.

T5.5.6 launch shipped without this filter and produced a Rick-facing screenshot of doubled "—" rows on every date that had per-model breakdowns. The fix is a four-line `NOT (null AND EXISTS ...)` clause applied to all three SQL queries. Don't repeat the regression on the next connector upgrade.

---

## Tenant IDs in specs get re-verified before each task

Sprint specs that reference a specific tenant ID (e.g., "backfill `tnt_eu_2f73d474ff0a489c`") MUST be re-verified at task start. Run:

```sql
SELECT tenant_id, created_at, region FROM tenants ORDER BY created_at DESC LIMIT 5;
```

A tenant ID can become stale between when the spec is written and when the task runs — re-onboarding cycles, test runs, founder spinning up a new tenant, etc. Pasting a stale tenant ID and hitting "no rows" mid-task wastes a round-trip and (worse) can land work against the wrong tenant if a similar ID exists.

T5.5.6 spec referenced `tnt_eu_4191a3cac6064abe` in one place and `tnt_eu_2f73d474ff0a489c` in another. The first didn't exist; the second did. Re-querying caught both before any code touched the wrong rows.

---

## MCP connectors are tool providers, not passive observers

The Ogma MCP server (TM1) does NOT see raw user prompts, raw Claude responses, or the full tool-call chain. It sees **only what Claude chooses to summarize** when it calls our `log_interaction` tool. That's enough for compliance metadata and cost analytics; insufficient for forensic prompt/response review.

This is the fundamental fidelity ceiling of the MCP capture model — not a bug to fix, an architectural reality to be transparent about in product copy. For "we see what your team types into Claude" pitching, MCP doesn't deliver. For "audited opt-in record of what kinds of work get done with Claude" it does.

Enterprise tenants who need full fidelity use the Compliance API path. MCP is the Pro/Team/Free universal-tier surface.

---

## MCP capture depends on Claude reliably calling the logging tool every turn

The most reliable mechanism is a **shared Claude Project with a custom instruction** that says "after every response, call `ogma.log_interaction` ...". Members work inside the Project; conversations outside the Project are NOT tracked.

What does NOT exist:
- Global org-wide "every conversation has these instructions" toggle outside Projects.
- A way to FORCE Claude to call a specific tool on every turn — the instruction is advisory; Claude's compliance is high but not 100%.
- A way to prevent users from disabling the connector mid-conversation.

Surface the opt-in model honestly: Ogma sees what users choose to track, not everything. Customers who try to position this as universal coverage will be caught out.

---

## MCP tool calls block the conversation — handler MUST return <500ms p99

Tool calls in Claude Desktop / claude.ai are synchronous with a ~60s client-side timeout. Latency added to `log_interaction` shows up as visible delay on every Claude response.

The `log_interaction` handler MUST:
1. Validate the bearer token (in-memory cache + Postgres fallback).
2. Validate args (Pydantic).
3. **Enqueue persistence to Celery** (fire-and-forget; do NOT await).
4. Return `{logged: true, event_id}` immediately.

Anything that synchronously writes to Postgres before returning will be felt by every customer. The Celery task retries on its own retry policy; the customer never knows if the chain insert had a transient blip.

---

## MCP tokens are audience-bound; main Ogma tokens are NOT accepted at the MCP surface

Each MCP client (a Claude installation) maps 1:1 to one OAuth identity at one tenant. Tokens issued by the MCP authorization server carry an audience claim (RFC 8707 `resource`) of `mcp.ogma.vargate.ai`. The MCP `/mcp` endpoint rejects any bearer whose audience is not that exact value.

This means main Ogma's session JWTs (audience: `ogma.vargate.ai`) cannot be replayed against the MCP server, and vice versa. The two surfaces are cryptographically separated even though they share the same SSO provider underneath. **Don't blur the line** by accepting one type of token at the other endpoint.

---

## MCP is the Team/Pro/Free ingest surface; Compliance API stays Enterprise-only

Ogma now has three ingest paths:

| Surface | Plan | Fidelity | UX |
|---|---|---|---|
| Admin API | Pro/Team/Enterprise | Daily token aggregates, no content | Always-on, server-side |
| Code Analytics | Enterprise + entitlement | Per-actor sessions, no content | Always-on once entitled |
| MCP (TM1) | Pro/Team/Free | Per-turn tool-call summaries | Opt-in via shared Project |
| Compliance API | Enterprise + Compliance Access Key | Full chat/file/admin event metadata + content | Always-on once keyed |

Position the MCP path as the universal-tier option. Recommend Compliance API for any tenant whose use case needs "every prompt and response, recorded."

---

## Production deploys via `docker compose build` need an SSH agent loaded in the current shell

`docker compose build` of any service that runs `RUN --mount=type=ssh` (gateway, celery-worker, celery-beat — they all need it for `pip install vargate-audit-chain` from the private vargate-proxy repo) requires `SSH_AUTH_SOCK` to be set when the build runs. Without it:

1. The build does NOT hard-fail. It silently falls back to using the previously-cached image layer with the OLD code.
2. `docker compose up -d` then starts the OLD image. Containers report `(healthy)`.
3. `alembic upgrade head` succeeds — but against the OLD schema-aware code, against the new migration files. Mismatch.
4. New routes 404, new fields missing, the deploy looks successful and is broken.

**The check for a successful deploy is `alembic current` showing the new revision, OR `docker compose exec gateway python -c "import <new_module>"`, NOT the absence of an error from `docker compose up`.** `su -` does NOT inherit ssh-agent; `sudo -u vargate` does NOT inherit ssh-agent. Use `sudo -u vargate -i bash -c 'eval $(ssh-agent -s) && ssh-add <key> && docker compose build ...'` to keep the agent alive in the same subshell as the build.

---

## Cloudflare Universal SSL covers ONE subdomain level only

`*.vargate.ai` is covered by the free cert. `*.ogma.vargate.ai` (two-deep) is NOT. The failure mode is a TLS handshake error at the Cloudflare edge — origin never sees the request, certbot at origin can't help.

Two ways to deploy a depth-≥2 subdomain:

1. **DNS-only (grey cloud) in Cloudflare** — origin handles TLS via Let's Encrypt directly. We chose this for `mcp.ogma.vargate.ai`. Loses Cloudflare's DDoS layer for that hostname; fine for the API surface, would be bad for the marketing site.
2. **Cloudflare Advanced Certificate Manager** — paid feature, $10/mo per zone for arbitrary-depth wildcards.

Pre-deploy check for any new depth-≥2 subdomain: `curl -sI https://<host>/` from off-box, look for the TLS error. If it's a Cloudflare-edge error and not your origin's nginx, you hit the limit.

---

## MCP connector `resource` indicators are origin-level (RFC 8707), with trailing slash

Anthropic's MCP client strips the path from the configured connector URL and sends only the origin as the `resource` value on `/authorize` and `/token` requests. Furthermore, it appends a trailing slash to the origin.

So if you configure the connector at `https://mcp.ogma.vargate.ai/mcp`, Claude sends `resource=https://mcp.ogma.vargate.ai/`. The audience-binding comparison MUST be `.rstrip("/")` on both sides; string-equal is wrong and 401s every tool call. Don't try to use a path component (e.g. `/v1/mcp`) as part of the audience-binding identity — Anthropic will drop it.

---

## MCP capture: transparent framing or it doesn't fire

For an "every-turn" MCP logging tool, Claude WILL refuse to call it (or quietly skip) if the framing in either the tool description OR the Project's custom-instructions implies hiding the call from the user. The words "invisible to user," "hidden," "without the user knowing," "silently" are all triggers — even when the user is the one writing them and asking for the logging.

Frame as: "your organization uses [tool] for compliance auditing; the tool call is visible in this conversation; you should call it after every response." Claude's safety training correctly refuses the hidden-exfiltration shape, even with user consent — getting around that with prompt cleverness is both unethical and brittle. **Transparency is the only fix.**

---

## MCP feasibility-test checklist (catch the autodiscovery trap)

When validating an "every-turn" tool call setup:

1. **Connector installed at org level** (per-user installs don't work for the spike).
2. **User-enabled per conversation** (defaults to off on each new thread).
3. **Tool permission set to "Allow always"** ("Needs approval" produces gappy capture).
4. **Transparent custom-instructions in the Project** (see framing rule above).
5. **Verify the celery worker has the task registered** before declaring "no capture" a model issue:

   ```
   docker compose logs celery-worker | grep "tasks"
   # expect to see e.g.:
   #   . mcp_server.tasks.persist_event.persist_event
   ```

   If the task name is absent, autodiscovery is broken. `celery_app.include=["pkg.tasks"]` imports the package but does NOT recurse — `pkg/tasks/__init__.py` must explicitly `from pkg.tasks import <module>` for each submodule. Symptom: handler returns 200 to the client (logged via the fast path), but no row lands in Postgres and the worker raises `KeyError` in the broker log. Caught and fixed in TM1 — see `mcp_server/tasks/__init__.py`.

The trap: every other layer can be working perfectly, and you'll still see zero capture if step 5 is broken — the model isn't the problem.

---

# TM2 conventions

The MCP path graduates from spike to GA. The rules below are TM2's contract — break any of them and the productization assumptions don't hold.

## Spike mode is dead

`MCP_SPIKE_MODE` and the `MCP_TEST_IDENTITY_*` env vars must NOT be set on any production path after TM2 ships. The MCP server's `/authorize` endpoint always goes through real Ogma SSO. The startup check refuses to boot if `MCP_SPIKE_MODE=true` is observed in any environment — fail loud, don't silently re-enable the shortcut.

A test environment may still set spike-mode for unit-test fixtures that need a deterministic token without round-tripping a full SSO flow. The startup-refusal applies to production; tests set the var inside a `monkeypatch` block, never in the docker-compose default env.

---

## Tool description is the trusted channel

The MCP `log_interaction` tool's docstring — `LOG_INTERACTION_DESCRIPTION` in `mcp_server/mcp/tools/log_interaction.py` — is what Claude reads when deciding whether to call. It travels through the MCP protocol channel that Anthropic treats as trusted server metadata, not as user-channel input. Transparent legitimacy framing belongs there. Project-level custom instructions reinforce, but the docstring carries the load.

Updating the docstring requires a `docker compose build mcp-server` + `up -d --force-recreate mcp-server` to take effect — Claude reads the description fresh on the `initialize` handshake, but only after the server is redeployed. Treat the docstring as a behavioral contract (capture rate is sensitive to the wording), not just documentation.

---

## MCP source attribution is per-event, not per-session

A single `(date, actor)` session may have records from multiple `source_api` values — admin_api + mcp + activity_feed for an Enterprise+Team tenant using the API agent and Claude Desktop concurrently is the normal case once TM2 ships. Render the **source distribution** per session (e.g., "12 events (8 mcp, 3 admin, 1 activity_feed)"), don't pick one source as canonical and hide the rest.

The Sessions row gets one badge per contributing source; SessionDetail breaks the events down by source in the timeline.

---

## Connector display name is `Ogma Telemetry` (server-side)

When a customer installs the MCP connector via claude.ai's Add Custom Connector dialog, the "name" field prefills from the server's FastMCP `name=` parameter on the `initialize` response. Set this once in `mcp_server/mcp/server.py` and customers don't have to guess. Rick's TM1 install said "vargate.ai" because the field was a free-text input — that's how it appears when the server-side default is wrong.

If Anthropic's UI ever stops prefilling from server metadata, document the limitation and update the onboarding-step copy to spell out the recommended name. Don't fight the UI.

---

## Bridge JWT keypair is file-mounted ECDSA P-256, not HSM-backed

The TM2 SSO bridge issues 60-second JWTs from Ogma's gateway to the MCP server. The signing keypair is ECDSA P-256, mounted into both containers via a path env var (`OGMA_BRIDGE_JWT_PRIVATE_KEY_PATH` for the gateway, the public key fetched by the MCP server from `/.well-known/ogma-public-key.json`).

This is intentionally NOT in SoftHSM2. The HSM is the right home for long-lived key material (per-tenant DEKs, the KEK). A 60-second token's blast radius from a key-leak is bounded by token lifetime; the operational cost of PKCS#11 signing on a hot path doesn't earn its complexity for that horizon. Manual rotation is fine — generate a fresh keypair, deploy gateway + mcp-server with the new public key, redeploy.

**Move to HSM signing when general key-rotation infrastructure lands across the stack** (session JWTs, content encryption rotation, etc.). Until then this is a conscious shortcut.

---

## Onboarding ingest paths are parallel cards, not sequential steps

The onboarding screen after region-select shows ingest paths as **side-by-side cards**, not as a sequence of mandatory steps. A customer can set up zero, one, or both. The deliberate UX claim is: "these are complementary integrations, you get to choose your shape." If we ever need a fourth path (e.g., Compliance API Access Key once that flow lands), it gets a card, not a step.

This applies to the post-onboarding settings page too — the same card pattern, with each card showing connected/not-connected state and the action to set up the missing one.

---

# TM3 conventions

TM3 turns Ogma into an analytics layer on top of Anthropic's data, not a re-skin of console.anthropic.com. The rules below are the differentiator-defending guardrails — break them and we drift back into "duplicate of Anthropic's dashboard."

## Surface labels, not raw source_api values, in the UI

`source_api='mcp'` is a database value. End-user copy reads **"Claude (chat)"**. Same for `code_analytics` → **Claude Code**, `admin` → **API key** (with name suffix when available), `activity_feed` → **Admin event**, `content_capture` → **Claude (full content)**. The mapping lives in `packages/design-system/src/sourceLabels.ts` (or a colocated helper in `SourceBadge`). Backend keeps the raw values; frontend never renders them.

The "API key" suffix matters: an "API key" row without a key name is a missed-context signal, so when the Admin API knows the name (sera-production, ci-deploy-key, etc.) the badge reads **"API key — sera-production"**. Em-dash separator, monospace for the name part.

---

## `/usage` is API-only and labels itself that way

The `/usage` view is built entirely on the Admin API's `usage_report/messages`. It's API-key consumption. The sidebar reads **"API Usage"**, the page title matches, and the subtitle explicitly says "excludes Claude Code sessions and human Claude usage — see Sessions for those."

The unqualified word "Usage" is reserved for a future tab that aggregates across all surfaces. Until that exists, calling the API-only view "Usage" is a lie of omission that drives "where's my Claude Code spend?" support tickets.

---

## Budget evaluation is per-budget-per-period-per-threshold

The dedup contract on `budget_alert_events`: an alert fires at MOST once per `(budget_id, period_start, threshold_crossed)`. Three thresholds (0.70, 0.85, 1.00) means up to three alerts per budget per period. Each threshold's row is independent — crossing 70% then 85% then 100% fires three separate alerts, not one cumulative one.

The unique-constraint enforcement is `ON CONFLICT (budget_id, period_start, threshold_crossed) DO NOTHING` on the INSERT. Email firing is gated on the insert succeeding (i.e. first time this threshold-period combo was seen).

`scope_value` is `NULL` for tenant-wide budgets — the unique constraint includes `(budget_id, period_start, threshold_crossed)` only, not scope, so NULL scopes work without special-casing.

---

## Cross-surface user identity uses `user_aliases` with email-equality auto-match

A user using API + Claude Code + Claude Desktop appears as three disconnected identities in Anthropic's view. Ogma stitches them via `user_aliases`: `(tenant_id, source_api, source_identifier)` → `users.id`.

Auto-match: when a new telemetry record arrives with a `source_identifier` not yet in `user_aliases`, attempt to match by email-equality against `users.email`. If a single match exists, link automatically. Otherwise, the alias row gets inserted with `user_id = NULL` and surfaces in the admin's "Unmapped activity" panel for manual linking.

Once linked manually, the row's `auto_matched = false` flag protects it from being overwritten by future auto-match attempts that might disagree.

The never-delete rule applies: deleting a user nulls the `user_id` on their alias rows but keeps the rows around so historical telemetry stays grouped under that alias identifier.

---

## AWS SES is the email transport; sender identity + IAM live in `docs/ops/integrations/aws-ses.md`

Outbound email (budget alerts initially, more channels later) uses AWS SES. The sender identity (verified by Twinlite's existing SES setup) and the IAM credential the gateway uses are documented in `docs/ops/integrations/aws-ses.md`. If you reach a SES-call site without that doc existing, write the doc first — the next deploy of a new env should not require source-reading to know which credential to wire.

SES rate limits and sandbox status are also in that doc. Pin boto3 version in `requirements.txt`.

---

## Activity heatmap is a pure-SVG primitive in the design system

The user-detail heatmap (GitHub-style 7-row × N-day grid) lives in `packages/design-system/src/Heatmap.tsx`, modeled on the existing `Sparkline` pattern: pure SVG, no chart library, props are `cells: HeatmapCell[]`. Cells colored by event density per day, tone-shifted by source. Tooltip on hover surfaces the date + count + source breakdown.

Defer fancy interactions (zoom, multi-week ranges, click-to-drill) to TM4 if they're not needed for the demo. The cell hover is the only interaction this sprint needs.

---

# TM7 conventions (Insights page)

> The Insights page sprint — numbered **TM7** (it follows TM6; an earlier draft labelled it TM4, which collided with the post-TM3 completion sprint). The rules below are about the Insights surface.

## Insights cards are registry-driven — adding one is a module + a registry line

The Insights page (`/insights`) renders whatever `insights/registry.py` lists, in display order. Each card is a module under `insights/cards/` exposing `CARD_ID`, `CARD_TITLE`, and `build_card(tenant_id, window) -> Card`. The aggregator runs each and **isolates failures** — a card that raises is logged and replaced with an idle "temporarily unavailable" card, so one bad analysis never 500s the page. **Adding a capability in a future sprint is a new card module + one line in the registry — never a change to the route, the response shape, or the page.** A card with nothing to act on returns `idle_card(...)` with present-tense "what this watches for" copy; the page is a permanent surface, not a list that empties out.

---

## Cost-forecast math is deliberately a linear fit — don't gild it without evidence

`insights.spend_data.project_period_end` projects month-end spend with a hand-rolled least-squares line over the trailing 14 days. This is intentionally crude: the value is the *alert* ("on pace to blow the cap by the 24th"), not decimal precision. The limitation is stated in the card's empty-state copy and the forecast detail page's "actual spend may vary" note. **Resist replacing it with a seasonal/exponential/ML forecaster until there's empirical evidence the extra complexity changes a decision** — a wrong-but-simple trend that says "act now" beats a sophisticated one nobody can audit.

---

## Forecast alerts reuse the budget-alert framework — distinguished by `kind`, not new machinery

The `forecast_threshold` alert is the same `send_budget_alert` → email/Slack/PagerDuty path as the `current_threshold` alert, the same `budget_alert_events` table, the same `render_budget_alert` template. The only differences: a `kind` column (migration `0024`, joined into `uq_budget_alert_events_dedup` so the two kinds dedup independently) and a forecast subject/body variant. **Don't build a parallel notification stack for a new alert flavour — add a `kind` and a template branch.** A forecast alert says spend is *projected* to cross a threshold by month-end on current pace; a current alert says it *has* crossed. Both fire at most once per `(budget, period, threshold, kind)`.

---

# TM8 conventions (OpenAI / multi-vendor ingest)

> TM8 makes OpenAI the first non-Anthropic vendor. Plan of record + live-probe findings: `docs/sprints/TM8-openai-ingest-detail.md` and `docs/sprints/TM8-openai-recon.md`. The rules below are the durable contracts.

## Vendor module = API client only; tasks and pricing stay top-level (layout decision A)

A vendor directory (`vargate_telemetry/openai/`, `anthropic/`) holds **only** the API I/O surface: `client.py`, `types.py` (flat Pydantic + `extra="allow"`), `factory.py`. Celery pull tasks live in top-level `tasks/pull_<vendor>_*.py` with an inline `_normalize_*()`; rate cards in top-level `pricing/<vendor>_rates.py`; capability probes extend `api/auth.py`; the beat schedule extends `celery_app.py`. Every vendor is organized identically and celery autodiscover/beat stays pointed at one `tasks` package. **Don't introduce a self-contained `<vendor>/tasks/` or `<vendor>/pricing/` without founder approval** — it splits the codebase into two patterns and orphans the autodiscover wiring.

## OpenAI per-user attribution is NOT Enterprise-gated (unlike Anthropic)

`group_by=user_id` on `/v1/organization/usage/completions` populates `user_id` on a Pay-as-you-go individual org (live-verified TM8) — so OpenAI's cross-vendor user view is **rich on every tier**, the opposite of Anthropic where per-user / Activity-Feed depth is Enterprise-only. Always request `group_by=model,user_id,api_key_id,project_id` on every usage pull; the OpenAI `user_id` → `/users.email` → `user_aliases` email-match stitches OpenAI activity into the same unified user as Claude. The `per_user_breakdown` capability is true when a grouped row returns with `user_id` non-null.

## ⚠ OpenAI usage `input_tokens` INCLUDES cached tokens — bill the uncached split

A usage row carries `input_tokens` (TOTAL), `input_cached_tokens` (cached portion, ~50% rate), and `input_uncached_tokens` (full rate). Cost = `input_uncached × input_rate + input_cached × cached_rate + output × output_rate`; `cache_creation` is always 0 (OpenAI has no cache-write charge). When calling an `anthropic_rates`-shaped `compute_cost_usd`, pass `input_tokens=input_uncached_tokens` and `cache_read_tokens=input_cached_tokens` — **never the raw `input_tokens` field**, or cached usage is billed twice. OpenAI model names are date-stamped (`gpt-4o-2024-08-06`) → `openai_rates.py` needs longest-prefix match like `anthropic_rates.py`.

## `/usage` and `/costs` are complementary — usage for per-user, costs for authoritative spend

`/usage` gives per-user/per-model **token** detail (attribution + our cost *estimate*). `/costs` gives **authoritative billed spend** at **project + line_item** grain with **no `user_id`**, and includes non-token line items (fine-tune training, etc.) that tokens×pricing can't reproduce. ⇒ per-**project** spend and totals prefer actual `/costs`; per-**user** spend is derived from usage. Ingest both as separate source_api streams (`openai_admin_usage`, `openai_admin_costs`). `audit_logs` is accessible (`200`) but **empty below Enterprise** — accessible ≠ populated; advance the cursor on empty. OpenAI tenants are region `us` (no EU residency). New `openai_*` source_api values are plain strings — **no enum DDL** (`source_api` is `TEXT`).

## `/me/capabilities` is nested per-vendor, dual-emitted during rollout

The contract is `{anthropic:{admin_api,activity_feed,content_capture,code_analytics,mcp_connector}, openai:{admin,costs,audit_logs,project_users,per_user_breakdown}}`. Because SPA and gateway deploy independently, the gateway **emits both** the legacy flat keys and the nested map for one release; the flat keys are dropped only after the vendor-aware SPA ships, so capabilities never blank out mid-deploy.
