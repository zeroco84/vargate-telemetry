# Cache-efficiency recommendations (TM5 T5.5)

Prompt caching can cut input-token cost substantially, but only if the
cache that's *written* (creation) actually gets *reused* (read). Ogma
already captures the cache token counters in every Admin-API usage
record; this feature surfaces a per-model verdict so a customer knows
where caching is helping and where it's being paid for but wasted.

**Pure analysis** — no ingest, no schema. It reads the same
`telemetry_records` (`record_type='usage'`, `source_api='admin'`,
`metadata.results[]`) the `/usage` view reads.

## The metric

Each usage breakdown splits input into three buckets:

- `input_tokens` — uncached input (full price).
- `cache_read_input_tokens` — cache hits (cheap; the reuse).
- `cache_creation_input_tokens` — cache writes (a premium over base
  input; the cost of *establishing* the cache).

**Cache hit rate** = `cache_read / (cache_read + cache_creation)` — of
all the cache activity, how much was reuse vs. one-time writes. A low
hit rate means you're paying the write premium without getting the read
discount back (unstable prefix, or cache expiring before reuse).

## Endpoint

`GET /usage/cache-recommendations?since&until` (defaults to last 30
days). Returns per model: token buckets, `cache_hit_rate`, a
`severity` (`ok` / `info` / `warn`), and a plain-English
`recommendation`; plus a tenant-wide `overall_hit_rate`. Models sort
most-actionable first. The tiers (in `api/usage.py::_cache_recommendation`):

| Condition (over the window) | Severity | Recommendation |
|---|---|---|
| Total input < 100k tokens | `ok` | Low volume — caching impact minimal yet. |
| Input ≥ floor, **no cache activity** | `warn` | No prompt caching detected — enabling it could cut input cost. |
| Hit rate < 50% | `warn` | Low reuse — stabilize the cached prefix / raise the cache TTL. |
| Hit rate 50–80% | `info` | Moderate — room to cache/reuse more of the prefix. |
| Hit rate ≥ 80% | `ok` | Healthy reuse. |

(The 100k-token floor avoids nagging on noise; thresholds live in one
pure function so they're unit-tested without a DB.)

## Dashboard

The **API Usage** page shows a "Cache efficiency" panel above the table:
the overall hit rate + the actionable models (warn/info) with their
recommendation, color-tagged by severity. When every model is healthy it
collapses to a one-line "looks healthy" note; with no usage in the
window the panel is absent. Same window as the page's date filter.

## Double-counting guard

The aggregation applies the same supersession filter as `/usage`: on a
date that has per-model breakdown rows, the legacy `model=null` aggregate
rows are excluded, so tokens aren't counted twice.

## Reference

- `vargate_telemetry/api/usage.py` — `getCacheRecommendations` endpoint +
  `_cache_recommendation` (the pure tier function).
- Frontend: `apps/ogma-dashboard/src/pages/dashboard/Usage.tsx`
  (`CacheRecommendationsPanel`), `src/lib/usage.ts`
  (`getCacheRecommendations`).
