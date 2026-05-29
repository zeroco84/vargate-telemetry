# Topic classification — Anthropic Haiku integration (TM4 Track D)

Ogma infers an **activity topic** for each MCP interaction so the user
detail page can show a "Top topics" breakdown (what a person actually
spends their Claude time on). Classification is done by **Claude Haiku**
(`claude-haiku-4-5`) over the interaction *summary* that Claude itself
self-reports through the MCP capture path.

This is the **only** place Ogma sends captured interaction content to an
external API. Everything else in the telemetry pipeline is local
(Postgres / Redis / MinIO). The data flow below is the authoritative
description of what leaves the box and why.

## What gets sent, and what's stored

| | |
|---|---|
| **Input to the model** | The free-text `summary` field of MCP `telemetry_records` (`metadata->>'summary'`). Summaries are Claude's own one-line descriptions of an interaction — not raw transcripts, not tool arguments, not credentials. |
| **Sent to** | Anthropic Messages API, model `claude-haiku-4-5`, via the official `anthropic` Python SDK. |
| **Returned** | One topic label per summary, constrained to a fixed enum (see taxonomy). |
| **Stored** | The label only, in the `interaction_topics` side table — `(tenant_id, record_id, topic, taxonomy_version, model, classified_at)`. The summary is **not** copied there; it already lives on the record. |
| **NOT stored / NOT sent** | No raw conversation content, no tool I/O, no PII beyond whatever the self-reported summary already contains. The classification never touches the hash-chained record — it's a decoupled side table (no FK to `telemetry_records`), so it can be recomputed or dropped without affecting the audit chain. |

Records with no summary are never candidates — there's nothing to send.

## Taxonomy (versioned, fixed)

`vargate_telemetry/topics/taxonomy.py` defines `TAXONOMY_VERSION = "v1"`
and a **fixed, curated** category set. The model is constrained to this
enum via structured output, so it cannot invent categories:

`Coding` · `Data & analysis` · `Writing & content` · `Research` ·
`Ops & infra` · `Planning & PM` · `Communication` ·
`Learning & explanation` · `Review & QA` · `Other`

Every stored row carries the `taxonomy_version` it was classified under.
When the taxonomy changes, bump the version; old rows keep their old
version and can be re-classified deliberately rather than silently
re-interpreted.

**Never fake a label.** If the model returns something off-enum or
empty, `normalize()` maps it to `Other` — it never guesses a specific
category. If a whole batch's API call fails, those records stay
unclassified and are retried next tick (no row is written). The UI shows
"N of M interactions classified" so partial coverage is honest, never
disguised as complete.

## Where the pieces live

| Component | Path |
|---|---|
| Taxonomy + `normalize()`/`is_valid()` | `vargate_telemetry/topics/taxonomy.py` |
| Classifier (SDK call, structured output, batching) | `vargate_telemetry/topics/classifier.py` |
| Per-tenant Celery task + beat dispatcher | `vargate_telemetry/tasks/classify_topics.py` |
| Side table migration | `migrations/versions/0021_create_interaction_topics.py` |
| `/users/{id}` `top_topics` aggregation | `vargate_telemetry/api/users.py` |
| Dashboard "Top topics" panel | `vargate-frontend` → `apps/ogma-dashboard/.../UserDetail.tsx` |

The classifier batches up to `BATCH_SIZE = 20` summaries per API call
(one request classifies many summaries — the cost lever, since Haiku's
minimum cacheable prefix is 4096 tokens so taxonomy prompt-caching does
not engage at this size). Each per-tenant run is capped at
`CLASSIFY_RUN_LIMIT = 200` records so a large backfill drains over
successive ticks instead of one huge task.

## Scheduling

`dispatch_classify_topics` runs on Celery beat every **900 s**, mirroring
the `evaluate_budgets` dispatcher: it enumerates active tenants in the
current region and queues one `classify_topics_for_tenant` per tenant.
The task runs both **forward** (newly-arrived MCP records) and as a
**backfill** (older records that predate classification), bounded by the
per-tick cap.

> **Region caveat.** Like every dispatcher, beat defaults to
> `VARGATE_REGION=us`. The EU tenants that hold the real data get no
> automatic dispatch until that gap is closed (see
> `ogma_dispatch_region_gap`). For an EU tenant, trigger directly:
>
> ```
> docker compose -f docker-compose.yml -f docker-compose.prod.yml \
>   exec -T celery-worker python -c \
>   "from vargate_telemetry.tasks.classify_topics import classify_topics_for_tenant; \
>    print(classify_topics_for_tenant('tnt_eu_...'))"
> ```
>
> This runs synchronously in the worker (which holds the API key) and
> returns `{candidates, classified, unclassified}`.

## The API key — `OGMA_ANTHROPIC_API_KEY`

The classifier reads the **SDK-standard `ANTHROPIC_API_KEY`** from the
worker container's environment. But in `.env` and `docker-compose.yml`
the value is sourced from a **namespaced** variable:

```
# .env
OGMA_ANTHROPIC_API_KEY=sk-ant-...

# docker-compose.yml (celery-worker only)
ANTHROPIC_API_KEY: ${OGMA_ANTHROPIC_API_KEY:-}
```

The container still sees `ANTHROPIC_API_KEY` (what the `anthropic` SDK
reads by default); only the **`.env` source name** is namespaced. It is
wired on the **worker only** — that's where classification runs. Beat
just `.delay()`s tasks; the gateway only reads already-classified rows.
Neither needs the key.

**History / footgun.** The first deploy used the bare
`ANTHROPIC_API_KEY` as the `.env` source name and silently shipped an
**empty** key to the worker — the classifier would have aborted with
`ClassifierNotConfigured` on every run. Cause: compose `${VAR}`
interpolation **always prefers a variable already present in the deploy
shell** over the `.env` file, and the deploy shell (an agent/CLI
environment) had `ANTHROPIC_API_KEY` exported empty, shadowing the real
108-char `.env` value. The pre-existing `ANTHROPIC_ADMIN_KEY_TEST`
(Admin-API client) never hit this precisely because it was already
namespaced. Lesson: **secrets consumed via compose interpolation must
use a namespaced source var** that ambient tooling won't collide with.
Verify after any worker recreate:

```
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec -T celery-worker sh -c 'echo "len=${#ANTHROPIC_API_KEY}"'
# expect len=108 (a real sk-ant-... key), not len=0
```

## Cost

Haiku is the cheapest tier; summaries are short; 20 per request. A full
backfill of a demo tenant (tens of records) is a couple of requests and
fractions of a cent. Forward classification is a trickle. There is no
retry storm — failed batches wait for the next 900 s tick.
