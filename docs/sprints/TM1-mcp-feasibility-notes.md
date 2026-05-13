# TM1 — MCP Connector feasibility test notes

**Sprint dates:** 2026-05-13 to <fill-in-when-§6-runs>
**Sprint goal (from the plan):** validate that an in-Claude MCP
connector can deliver post-turn telemetry rows to Ogma at high
capture rate, with summaries good enough for adoption analytics,
without intolerable handler latency.

**§6 driver:** Rick, on prod box `vargate@204.168.135.95`, using
his personal Claude.ai Team plan + the connector wired to
`https://mcp.ogma.vargate.ai`.

This file is the **single source of truth for the Green / Yellow /
Red recommendation** that gates TM2. Fill it in as the §6 sessions
run. Don't summarize in the commit message — the founder reads this
doc, not the git log.

---

## ⚠ Spike-only decisions (TM2 MUST address before any non-spike use)

These shortcuts were taken in TM1 to make §6 reachable in a sprint.
They are NOT acceptable in production beyond the feasibility window.

1. **`MCP_SPIKE_MODE=true` env var bypasses real SSO.** The
   `/authorize` endpoint uses a STATIC identity from
   `MCP_TEST_IDENTITY_{TENANT_ID,USER_ID,USER_EMAIL}` instead of
   running an OAuth-bridged flow against the user's Ogma SSO
   session. The flow is correct on the wire (PKCE S256, audience
   binding, refresh rotation), but every authorize call resolves to
   the same identity. Logs emit a prominent `SPIKE MODE: …` WARNING
   on every call so the shortcut can't hide.
   - **TM2 must:** build the real SSO bridge described in TM1 §10
     (route Claude → ogma.vargate.ai/auth/mcp → existing SSO → back
     to mcp.ogma.vargate.ai/authorize with a one-time signed
     identity token). Production builds must run with
     `MCP_SPIKE_MODE` UNSET — the endpoint returns 501 otherwise.

2. **Static test identity for §6 must point at a single Rick-owned
   tenant.** Pick the smallest active US tenant the founder owns;
   do NOT use a customer tenant. The chain-of-trust on the resulting
   `telemetry_records` rows is "this tenant's bearer token; signed
   by our MCP-server private key" — perfectly fine for chain
   verification later, but the *identity* came from an env var, not
   from the user-in-front-of-the-laptop.

3. **No per-user authorization on `log_interaction`.** Every
   bearer token issued during the spike has scope
   `log_interaction` — there's no per-user gating. With the static
   identity, this is fine; with a real SSO bridge, TM2 should keep
   the scope granular so a future tool gets its own scope name.

4. **In-memory auth-code + refresh-token stores.** Process-local
   dicts with TTL GC. Fine for single-replica TM1. TM2 must move
   these to Redis (use the `pull_state` precedent) if/when we run
   more than one MCP replica.

5. **`MCP_TEST_IDENTITY_*` is read at request time.** If Rick
   changes the env var mid-session he gets a stale identity until
   the container restarts. Acceptable for the spike; the SSO
   bridge in TM2 removes the env var entirely.

---

## How to read this doc

§6 has three sessions. Each session has a numbered cell below.
Fill in as you go:

- Cell A: **Capture rate.** Number of `log_interaction` calls
  during the session ÷ number of Claude responses in the session.
  Target ≥ 80% (Green), 50–80% (Yellow), < 50% (Red).
- Cell B: **Summary quality.** Skim 20 random `summary` strings
  from the session. Are they useful for adoption analytics?
  Target ≥ 80% useful (Green), 50–80% (Yellow), < 50% (Red).
- Cell C: **Handler latency.** p50 and p99 of the MCP handler in
  ms. Read from `docker logs vargate-telemetry-mcp-server-1 |
  grep 'log_interaction enqueued'` — the gap between request time
  and log time is the handler latency. Target p99 < 500 ms (Green).
- Cell D: **Sample payloads.** Paste 3 representative
  `record_metadata` blobs verbatim. These let the founder eyeball
  what we're actually capturing.

---

## Session 1 — initial test

**Date / time:** <fill in>
**Conversation flavor (chat / coding / mixed):** <fill in>

### A. Capture rate

| Metric                                    | Value |
|-------------------------------------------|-------|
| Total Claude responses in session         |       |
| Total `log_interaction` calls received    |       |
| Capture rate                              |       |

**Reading:** Green / Yellow / Red — <one-line rationale>

### B. Summary quality

Sample 20 summaries, tally how many are useful (= captures the
gist of the turn in one sentence; would help a manager understand
what was done):

| Useful | Vague | Empty / wrong |
|--------|-------|---------------|
|        |       |               |

**Reading:** Green / Yellow / Red — <one-line rationale>

### C. Handler latency

| Metric         | Value (ms) |
|----------------|------------|
| p50 (handler)  |            |
| p99 (handler)  |            |
| p99 (Celery enqueue → DB row visible) |            |

**Reading:** Green / Yellow / Red — <one-line rationale>

### D. Sample payloads (paste 3 records' `metadata` JSON verbatim)

```json
// row 1
```

```json
// row 2
```

```json
// row 3
```

---

## Session 2 — coding-heavy run

**Date / time:**
**Conversation flavor:**

(repeat sections A–D)

---

## Session 3 — long / multi-turn

**Date / time:**
**Conversation flavor:**

(repeat sections A–D)

---

## Aggregate findings

Roll up across all three sessions:

| Metric                  | Aggregate |
|-------------------------|-----------|
| Total responses         |           |
| Total log_interaction   |           |
| Aggregate capture rate  |           |
| % useful summaries (60-sample roll-up) |           |
| Aggregate p99 handler latency           |           |

---

## Recommendation

**Green (proceed to TM2):**
- Capture rate ≥ 80%, summaries ≥ 80% useful, p99 < 500 ms.
- Build the real SSO bridge in TM2.
- Plan the UI surface in the Ogma dashboard.

**Yellow (iterate on the docstring + retry):**
- Capture rate 50–80% OR summaries 50–80% useful, but latency OK.
- Iterate on `LOG_INTERACTION_DESCRIPTION` and rerun §6 in TM1.x.
- The bottleneck is wording, not infrastructure.

**Red (re-scope the strategy):**
- Capture rate < 50% OR summaries < 50% useful OR p99 > 1000 ms.
- The MCP-as-tool surface isn't reliable enough for analytics.
- Consider the alternate strategy: ingest from the Anthropic
  Compliance API events instead (TM3 candidate), and treat MCP
  as an opt-in user-facing accelerator rather than the default
  ingest path.

**Recommendation:** <Green / Yellow / Red> — <signed by founder>

---

## What to do if §6 surfaces a bug not listed in the spike-only section

File a TM2 task in the sprint plan, link it from this doc, and
keep going. The spike's purpose is to learn what the production
TM2 needs to build — surfacing additional gaps is a successful
spike, not a failed one.
