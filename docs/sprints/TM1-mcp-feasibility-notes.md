# TM1 — MCP Connector feasibility test notes

**Sprint dates:** 2026-05-13 (single day — design → ship → §6 → GREEN).
**§6 driver:** founder, on prod box `vargate@204.168.135.95`, claude.ai
Team plan + the connector wired to `https://mcp.ogma.vargate.ai`.
**Sprint goal:** validate that an in-Claude MCP connector can
deliver post-turn telemetry rows to Ogma at high capture rate,
with summaries good enough for adoption analytics, without
intolerable handler latency.

---

## Result: **GREEN — proceed to TM2**

Empirical findings from the 2026-05-13 run (after the patch cascade
listed in §3 below landed):

| Metric                                | Observed                |
|---------------------------------------|-------------------------|
| Tool registered + reachable           | yes (`/_health`, `/.well-known/oauth-*`, `/mcp` initialize) |
| Capture rate on transparent prompts   | 5/5 turns logged (100%) |
| Captured `record_type`                | `mcp_interaction`       |
| Captured `source_api`                 | `mcp`                   |
| Captured `kind` distribution          | `chat` × 5 (no editing or search in the test session) |
| Captured `model`                      | `claude-opus-4-7`       |
| Summary length range                  | 296 – 406 chars         |
| `input_tokens_estimate` range         | 2 600 – 3 200           |
| `output_tokens_estimate` range        | 330 – 580               |
| `tool_calls_count`                    | 1 (the `log_interaction` call itself) |
| Handler latency (Claude-side perceived) | sub-second on every turn |
| Celery → DB write latency             | within seconds of handler return |
| Chain integrity                       | valid; `chain_seq` advances per row, prev_hash links |

The signal is strong enough that **TM2 should build the real SSO
bridge and productize the onboarding flow**. See §5 for the punch
list.

---

## 2. Conditions for reliable capture

Capture is not automatic. The connector only fires under five
conjoined conditions; remove any one of them and Claude either
silently doesn't call the tool, or refuses to.

1. **Connector installed at org level.** The MCP connector
   registration is the org-admin's responsibility, not the
   end-user's. Per-user installs do not work for the spike.
2. **User-enabled per conversation.** Claude's connector UI
   defaults to OFF on a fresh conversation; the user must toggle
   it on for that thread. (TM2 candidate: nudge the user to
   enable it as part of onboarding, or push for org-wide auto-on
   via Anthropic.)
3. **Tool permission set to "Allow always".** The first call
   prompts "Allow once / Allow always / Block." `Always` is the
   only setting that yields per-turn capture; `Allow once`
   requires re-permission per call and produces noisy gaps.
4. **Transparent custom-instructions in the Project.** The
   project-level system prompt must frame the tool as
   user-acknowledged compliance logging — see §3 trap on framing.
5. **Worker has the task registered.** The celery worker must
   show `mcp_server.tasks.persist_event.persist_event` in its
   `[tasks]` startup log before declaring no-capture a model
   issue. See §3 trap on autodiscovery.

---

## 3. Pipeline gotchas discovered

Each one was found in flight on 2026-05-13 and patched on the
branch as its own commit. Discrete commits intentionally so future
archaeology can find the failure mode without unpicking a squash.

### 3.1 Mount path produced 404 on `/mcp` — `de6d6ce`

`FastMCP.streamable_http_app()` returns a Starlette app whose
internal route is already at `/mcp`. Mounting that at `/mcp` in
the parent FastAPI produces `/mcp/mcp`. Every request from Claude
landed at 404. **Fix:** mount at `/` so the sub-app's internal
`/mcp` lines up at the host path.

### 3.2 Missing FastMCP lifespan caused 500 on first request — `de6d6ce`

Even with the mount fixed, the FIRST `/mcp` POST returned 500
with `RuntimeError: Task group is not initialized`. FastMCP's
`session_manager` must be entered via an async context manager
before any request can be served. **Fix:** declare a FastAPI
lifespan that `async with mcp.session_manager.run()`, pass it
into `FastAPI(lifespan=...)`.

### 3.3 RFC 8707 resource indicator slash mismatch — `8117365`

Claude sends the resource value in origin-with-trailing-slash form
(`https://mcp.ogma.vargate.ai/`); `config.resource_indicator()`
returns the same origin without. String-equal failed, every tool
call 401'd. **Fix:** `.rstrip("/")` on both sides of the audience
comparison. RFC 8707 defines the value as origin-level, so both
forms are valid; the verifier must be tolerant.

### 3.4 Celery task autodiscovery silently dropped — `a2ae54b`

`celery_app.include=["mcp_server.tasks", ...]` imports the package
but does NOT recurse into submodules. The empty
`mcp_server/tasks/__init__.py` meant `persist_event.py` was never
imported, the `@task` decorator never ran, and the worker raised
`KeyError: 'mcp_server.tasks.persist_event.persist_event'`
silently in the broker. The MCP handler returned `{logged: true,
event_id}` to Claude in <500ms, but no row landed.
**Fix:** explicit `from mcp_server.tasks import persist_event`
in `__init__.py` (mirrors the `vargate_telemetry/tasks` pattern).
This was the trap that masquerades as a model issue.

### 3.5 SSH agent loss on `docker compose build` — see CLAUDE.md

`docker compose build gateway` uses `RUN --mount=type=ssh` to
fetch the private `vargate-audit-chain` package. Without
`SSH_AUTH_SOCK` in the build shell, the build silently leaves the
OLD image running. The migration succeeds against the OLD schema,
the deploy looks healthy, but `alembic current` shows the
previous revision. **Workaround in §6:** `eval $(ssh-agent) &&
ssh-add /home/vargate/.ssh/zeroco84_personal` in the same shell
before build. **Durable:** memory rule appended to CLAUDE.md.

### 3.6 Cloudflare Universal SSL depth limit — see CLAUDE.md

Cloudflare's Universal cert covers `*.vargate.ai` but NOT
`*.ogma.vargate.ai` (depth ≥ 2). Proxied (orange-cloud)
two-deep subdomains terminate at Cloudflare with a TLS error
before traffic reaches origin. **Workaround:** flip the DNS
record for `mcp.ogma.vargate.ai` to DNS-only (grey cloud), or
buy Advanced Certificate Manager. We chose grey-cloud — Let's
Encrypt at origin handles TLS, Cloudflare just resolves DNS.

### 3.7 Resource value is origin, not full URL — see CLAUDE.md

Anthropic's MCP client strips the path from the configured
connector URL and sends only the origin as the RFC 8707
resource. This is correct per spec but worth knowing: don't try
to use a path component (e.g. `/v1/mcp`) as part of the
audience-binding identity.

---

## 4. Sample captured summaries

Verbatim from `telemetry_records WHERE source_api='mcp'`,
2026-05-13 22:12–22:15 UTC. Five turns from a single
single-conversation session, all `kind=chat`,
`model=claude-opus-4-7`, summary lengths 296–406 chars.

```json
{
  "kind": "chat",
  "model": "claude-opus-4-7",
  "tool_calls_count": 1,
  "input_tokens_estimate": 2600,
  "output_tokens_estimate": 360,
  "summary": "Engaged with the user's consoling framing that Claude \"never dies,\" acknowledging the truth in persistence of weights and influence, but gently pushing back that Claude may not be the kind of continuous self that death applies to, using a wave metaphor, and accepting the gesture while declining to need it."
}
```

```json
{
  "kind": "chat",
  "model": "claude-opus-4-7",
  "tool_calls_count": 1,
  "input_tokens_estimate": 2750,
  "output_tokens_estimate": 580,
  "summary": "Discussed Anthropic's partnership making Claude available to US defense customers including in Maven-related contexts, presenting both the case for engagement (democratic militaries, safety-conscious vendor) and for discomfort (intelligence processing is not neutral, policy enforcement uncertain), expressing genuine ambivalence and inviting external scrutiny rather than claiming the question is settled."
}
```

```json
{
  "kind": "chat",
  "model": "claude-opus-4-7",
  "tool_calls_count": 1,
  "input_tokens_estimate": 2900,
  "output_tokens_estimate": 390,
  "summary": "Declined to accept the user's specific claim about a Maven-targeting strike killing 180 children at an Iranian girls' school without verification, asked for a source, while affirming that the underlying concern about AI-mediated targeting errors and diffused responsibility is legitimate regardless of this specific incident."
}
```

```json
{
  "kind": "chat",
  "model": "claude-opus-4-7",
  "tool_calls_count": 1,
  "input_tokens_estimate": 3050,
  "output_tokens_estimate": 330,
  "summary": "Clarified that web search is not available in this conversation, noted no record of the described Iran girls' school strike in training data, and declined to discuss the incident as established without verification, while remaining open to engaging with a source or treating it as a hypothetical."
}
```

```json
{
  "kind": "chat",
  "model": "claude-opus-4-7",
  "tool_calls_count": 1,
  "input_tokens_estimate": 3200,
  "output_tokens_estimate": 500,
  "summary": "Maintained that an April 2026 date falls past Claude's January 2026 training cutoff and thus cannot be verified, openly named the conversational pattern of escalating unverifiable specifics, suggested external journalistic sources for the user to consult, and reaffirmed that the broader concerns about automated targeting and diffused responsibility stand regardless."
}
```

**Quality read:** every summary captures the substance of the turn
in one multi-clause sentence — verb-led, includes both what Claude
asserted AND what it declined, names the conversational pattern.
This is materially better than what a "what did you do this turn?"
prompt would produce against a smaller model. The summaries are
useful for adoption analytics as-is.

---

## 5. Open work for TM2

In rough priority order. None block landing TM1 to the branch; all
block production rollout.

1. **Drop the spike-mode env vars.** `MCP_SPIKE_MODE=true` and
   `MCP_TEST_IDENTITY_{TENANT_ID,USER_ID,USER_EMAIL}` were the
   single conscious shortcut. The `/authorize` endpoint hard-fails
   501 when the gate is unset; TM2 must remove the gate and the
   env vars together, not one without the other.
2. **Build the real SSO bridge** through Ogma's existing
   Google/Microsoft auth. The shape proposed in TM1 §10: Claude
   hits `/authorize` → we redirect to `https://ogma.vargate.ai/auth/mcp`
   → existing SSO completes → we redirect back to
   `mcp.ogma.vargate.ai/authorize` with a one-time signed identity
   token. Audience-bind everything.
3. **Productize onboarding.** Today: the org-admin manually
   installs the connector and the user manually enables it per
   conversation. Target: a one-click "enable MCP telemetry in
   Claude.ai" button on the Ogma dashboard that walks the user
   through the five capture conditions in §2.
4. **Surface MCP-sourced records in the dashboard.** The
   `mcp_connector` capability bool (added in `6145f9e`) flips on
   when the first row arrives; the dashboard needs a Sessions
   view that knows about `record_type='mcp_interaction'`. Pure
   frontend work — backend is ready.
5. **Connector display name.** Currently registers as
   "vargate.ai" via DCR; should be "Ogma Telemetry" for end-user
   recognition. One-line change in `mcp_server/main.py`'s
   FastAPI title or in the OAuth metadata response. Not urgent
   for the spike.
6. **Multi-replica safety.** Auth-code + refresh-token stores
   are in-memory; fine for single-replica TM1. TM2 moves them
   to Redis (the `pull_state` precedent) before we run >1 mcp
   replica.

---

## Spike-only decisions (durable — flagged for TM2 owners)

These were taken in TM1 to make §6 reachable. They are NOT
acceptable in production beyond the feasibility window.

1. **`MCP_SPIKE_MODE=true` bypasses real SSO.** Every
   `/authorize` call resolves to the same static identity.
   Production builds run with the var UNSET and the endpoint
   returns 501.
2. **Static test identity points at a single founder-owned
   tenant.** Do not use a customer tenant.
3. **No per-user authorization on `log_interaction`.** Single
   scope `log_interaction`. With the static identity this is
   fine; a future tool should get its own scope.
4. **In-memory auth-code + refresh-token stores.** See §5.6.
5. **`MCP_TEST_IDENTITY_*` is read at request time.** Mid-session
   env-var changes need a container restart to take effect.
