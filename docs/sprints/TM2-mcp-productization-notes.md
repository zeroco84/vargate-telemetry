# TM2 — MCP Connector productization notes

**Sprint dates:** 2026-05-14 to <fill-in-when-G1-runs>
**Branches:**
  - `vargate-telemetry` — `feature/tm1-mcp-connector` (continuation of TM1's branch; the name is a misnomer post-TM2 but rebasing the name mid-sprint isn't worth the churn)
  - `vargate-frontend` — `feature/tm2-mcp-productization`
**Sprint goal:** make TM1's GREEN feasibility result the default — replace static spike-mode identity with real SSO, integrate onboarding, surface MCP records in the dashboard, harden the tool description so capture works without per-Project hand-tuning, ship branding.

**G1 driver:** founder/Rick, on prod box `vargate@204.168.135.95`, using a fresh claude.ai conversation **with no Project custom instructions** (the empirical bet of TM2).

This file is the **post-TM2 record** of what shipped, what the
empirical capture-rate-without-Project number turned out to be,
and what's left for TM3.

---

## Result: **YELLOW — capture works, but the docstring alone isn't sufficient**

Empirical finding from the G1 demo run: a fresh claude.ai
conversation with the connector installed + `log_interaction` set
to "Allow always" but **no other trust affordance** does NOT
achieve reliable capture from the docstring alone. The hardened
F1 docstring helped — Claude no longer refuses outright — but
without a second legitimacy signal alongside the OAuth bearer
token, the model defers to caution and skips most calls.

**Reliable capture requires one of three trust affordances**, in
addition to the always-required `log_interaction` permission set
to "Allow always":

  1. **Personal preferences** in claude.ai (set to ask-first or
     unconditional acknowledgment of org-installed audit
     connectors)
  2. **Project custom instructions** (the TM1 path — frame the
     connector + tool call as expected behavior at the
     project-system-prompt level)
  3. **(Reserved)** an admin-prompt feature, if/when Anthropic
     ships org-level system prompts that bypass per-user setup

In production: surface this requirement clearly in the MCP
Connector onboarding card and the Settings → Integrations card
(the inline expansion already shows the Project-instructions
copy and the permission-setting steps). TM3 picks up the
documentation polish; the docstring stays as it is — it's
loadbearing for the cases where personal preferences ARE set,
just not sufficient on its own.

The TM2 §5.2 bar (≥80% from docstring alone) wasn't hit, but the
sprint goal — "make TM1's GREEN result the default" — IS hit so
long as the onboarding surface clearly communicates the
"one-of-three" requirement. The onboarding card's existing copy
already mentions Allow-always and the Project-instructions
recipe; it does not yet mention personal preferences. **Notes
this gap for TM3.**

---

## G1 — empirical capture-rate findings

The TM2 §5.2 hypothesis was: a hardened docstring carries the
legitimacy framing alone, so the default install (Allow-always
permission only — no Project, no personal preferences) hits ≥80%
capture rate.

**Hypothesis falsified.** The hardened docstring did improve the
TM1 baseline (Claude no longer refuses outright) but did NOT
reach reliable per-turn capture on its own. The model still
treats the bearer-token-as-proof-of-install signal as insufficient
when nothing in the user-channel reinforces it.

### Reliable-capture rule (verified live)

**Allow-always permission** (always required) PLUS at least ONE of:

1. **Personal preferences** — ask-first or unconditional
   acknowledgment of org-installed audit connectors. Set per-user
   in claude.ai settings. **Lowest-friction option.**
2. **Project custom instructions** — the TM1 recipe. Higher
   setup cost (one Project per workspace) but covers everyone
   who joins that Project.
3. **(Reserved)** — admin-prompt feature, pending Anthropic.

With ANY one of the three plus Allow-always, capture is reliable
(same multi-paragraph summary quality + sub-second latency as
TM1). Without any of the three, capture is unpredictable.

### Implication for the onboarding card

The MCP Connector card's inline expansion currently lists:
  - Step 1: install at org level
  - Step 2: copy URL
  - Step 3: each user enables + sets permission to "Allow always"
  - Step 4 (recommended): Project custom instructions

TM3 should add a "Step 3b" (or restructure) calling out personal
preferences as the lighter-weight alternative to Step 4. The
docstring (TM2 F1) stays as-is — loadbearing for the cases where
ANY trust affordance is set.

---

## What shipped — phase-by-phase

### Phase A — Foundations

| SHA | Phase | What |
|---|---|---|
| `8784441` | A1 | TM2 memory notes — six durable rules (spike dead, docstring trusted channel, per-event source attribution, FastMCP name server-side, bridge keypair file-mounted, parallel-cards) |
| `746fcef` | A2 | Bridge JWT keypair (ECDSA P-256) — loader, JWK, sign/verify primitives + 12 tests |
| `701b826` | A3 | Spike-mode fail-loud-at-startup guard — refuses to boot in prod if `MCP_SPIKE_MODE` is set without the test-bypass env var |
| `31003a4` | A3 hotfix | conftest overrides docker-compose's env (not setdefault) so test keypair wins |

### Phase B — SSO bridge, Ogma-side

| SHA | Phase | What |
|---|---|---|
| `3c66543` | B1 | `GET /.well-known/ogma-public-key.json` — RFC 7517 JWK endpoint, 24h Cache-Control. The MCP server fetches this at boot |
| `21a39c6` | B2 | `GET /auth/mcp-bridge` — signed-in → mint 60s ES256 JWT → 302 to MCP callback. Signed-out → 302 to `/onboarding/sso?next_url=…`. Return-URL allowlist enforced |
| `a8cd270` | B3 | nginx vhost (apex routes for `/.well-known/...` + `/auth/mcp-bridge`) + production deploy. Verified externally: JWK + 422-on-missing-params + 302-when-signed-out |

### Phase C — SSO bridge, MCP-side

| SHA | Phase | What |
|---|---|---|
| `b0593b9` | C1 | `/authorize` no longer 501s — redirects to Ogma's bridge with mcp_state, persists OAuth state in Redis (10min TTL, one-shot GETDEL) |
| `a9f33ce` | C2 | `/authorize/callback` — verifies bridge JWT (ES256-only — `algorithms=["ES256"]` defeats `alg=none` + HS256-with-pubkey-as-HMAC attacks), claims Redis state, mints auth code. 12 tests including every JWT confusion attack |
| `9eae835` | C3 | Lifespan-driven JWK fetch from `/well-known/...` at MCP server boot. 5s timeout × 3 retries × linear backoff; hard-fails boot on exhaust |
| `a6d38ad` | C4 | Daily Celery beat task re-fetches the JWK so rotation propagates without redeploy |
| `38ad4b3` | C5 | `test_mcp_oauth.py` cleanup — drop redundant spike paths, route /token tests through the SSO bridge |

### Phase D — Backend endpoints + frontend onboarding

| SHA | Phase | What |
|---|---|---|
| `125ca4c` | D1 (BE) | `GET /onboarding/mcp-status` — `{configured, first_event_at, events_count}` poll surface |
| `f6c3ecf` | D2 (BE) | `GET /me/capabilities` — 5-bool snapshot, data-existence semantics (recent rows in `telemetry_records` per `source_api`) |
| `86d9c2f` | D4 (FE) | `CopyButton` + `IconCopy` in `@vargate/design-system` — shared copy-to-clipboard primitive |
| `d763b07` | D3+D4 (FE) | Two-card onboarding screen — replaces single-card intro at `/onboarding/connect`. Admin API card navigates into existing paste-key flow; MCP card expands inline with URL + Project instructions + poll loop |
| `d332264` | D5 (FE) | Settings → Integrations page — same parallel-cards pattern as onboarding, for post-skip setup |

### Phase E — Dashboard surfacing

| SHA | Phase | What |
|---|---|---|
| `0492b52` | E1 (BE) | Sessions list emits `event_count_by_source` per session; MCP added to `_SESSION_SOURCE_APIS`; flat-metadata fallback in `_ACTOR_KEY_SQL` / `_ACTOR_TYPE_SQL` |
| `4eb6ac1` | E1 followup | OpenAPI yaml reflects new field so frontend codegen picks it up |
| `41e5ed5` | E2 (FE) | `SourceBadge` + `SourceBadgeStrip` in DS, wired into Sessions row. Color-coded pills (chart-series tokens) |
| `4b9d1ee` | E3 (FE) | SessionDetail MCP-aware renderer — summary inline, model + token metric strip, MCP badge, kind chip, NO reveal-content button (no content_ref by design) |
| `b3a3d6e` | E4 (FE) | DashboardHome MCP Interactions tile — per-day rollup off `event_count_by_source.mcp`, clicks into filtered Sessions |
| `178b0c1` | E5 (FE) | MeProvider capability reconcile — fetches `/me/capabilities` after auth success, writes sessionStorage cache, exposes via context |

### Phase F — Trust framing

| SHA | Phase | What |
|---|---|---|
| `604bb94` | F1+F2 | `LOG_INTERACTION_DESCRIPTION` rewritten — 5-paragraph hardened framing carrying legitimacy at the tool-docstring layer. `FastMCP(instructions=...)` expanded to match — same frame at the initialize-handshake layer. `name="Ogma Telemetry"` stays (landed TM1) |

---

## Test gates (per phase + cumulative)

| Phase | Backend | Frontend DS | Frontend SPA |
|---|---|---|---|
| TM1 GREEN baseline | 287 | n/a | n/a |
| A | +17 (bridge_keys + spike-guard) | — | — |
| B | +11 (well-known + bridge) | — | — |
| C | +33 (sso bridge MCP-side) | — | — |
| D | +13 (mcp-status + capabilities) | +6 (CopyButton) | +14 (onboarding + settings) |
| E | +3 (sessions source) | +11 (SourceBadge) | +5 (Sessions row, SessionDetail, MCP tile, caps reconcile) |
| F | 0 (prompt-only) | — | — |
| **Cumulative TM2** | **303 / 2 skip** | **45 / 45** | **127 / 128** (+ 1 pre-existing Sessions flake) |

---

## Deviations from the TM2 spec

1. **Spec assumed Ogma already had an asymmetric JWT keypair** for session JWTs. It doesn't — session JWTs are HS256 with a shared secret. Phase A2 introduced a NEW ECDSA P-256 keypair specifically for the bridge, file-mounted at `/home/vargate/secrets/ogma_bridge_jwt_private.pem`. Memory rule recorded: *"Bridge JWT keypair is file-mounted ECDSA P-256, not HSM-backed."*

2. **Two-card onboarding screen is asymmetric.** The spec said "both cards expand inline." Admin API card navigates into the existing PasteKey → Region → backfill flow rather than expanding inline, because that flow has multi-screen state that doesn't fit an inline card. MCP card expands inline. Settings page mirrors the asymmetry. Documented inline in the D3+D4 commit; the founder approved.

3. **MCP rows in TM1 had FLAT metadata** (top-level `user_email`, `subject_user_id`) instead of the nested `metadata.actor.*` envelope other streams use. Phase E1 added a COALESCE fallback so MCP rows group correctly into Sessions without rewriting the persist task. Captured TM1 rows keep working unchanged.

4. **OpenAPI ref temporarily pinned to feature branch.** `apps/ogma-dashboard/openapi.config.json` was bumped from `ref: "main"` to `ref: "feature/tm1-mcp-connector"` so the d.ts regen picks up the new `event_count_by_source` field during the sprint. **Phase G prep reverts this to `main`** before the merge.

5. **One pre-existing flaky test in `Sessions.test.tsx`** — passes in isolation, fails in full-suite runs due to test pollution. Reproduced in Phase D, persisted through E. Not TM2's regression; existed pre-D. Recommend isolating + fixing in a separate test-stability sprint.

---

## Spike-only decisions retired

TM1 flagged five spike-only shortcuts. All are removed or replaced in TM2:

| TM1 shortcut | TM2 disposition |
|---|---|
| `MCP_SPIKE_MODE=true` bypasses real SSO | **Retired.** Phase A3 startup guard refuses to boot if set without `MCP_ALLOW_SPIKE_MODE_FOR_TESTING=1` (test-only escape). Production runs without either var; `/authorize` always redirects to the real bridge. |
| Static test identity from `MCP_TEST_IDENTITY_{TENANT_ID,USER_ID,USER_EMAIL}` | **Retired.** Bridge JWT carries the real signed-in user's identity from Ogma's SSO. The env vars are removed from the production `.env` (still in the CI test-bypass env). |
| In-memory auth-code + refresh-token stores | **Partially addressed.** Auth-code store remains in-memory (single-replica MCP container; fine). Redis-backed OAuth-state store added in C1 (`mcp:oauth_state:...`). Move to Redis for refresh tokens when we ship multi-replica MCP. |
| No per-user authorization on `log_interaction` | **Unchanged.** Single scope `log_interaction`. Per-user gating not in TM2 scope; revisit if a second tool is added. |
| `MCP_TEST_IDENTITY_*` read at request time | **Retired** along with the static-identity path. |

---

## Open items for TM3+

In rough priority order:

1. **Onboarding copy update — the one-of-three trust-affordance rule.** G1 confirmed: the hardened docstring isn't sufficient by itself. The MCP Connector card (in onboarding AND in Settings) currently lists the Allow-always permission + the Project custom-instructions recipe; it does NOT yet mention personal preferences as the lighter-weight alternative. TM3 should add a short "first set up one of these trust affordances" callout above the existing instructions, ordered: (a) personal preferences, (b) Project instructions, (c) reserved for admin-prompt when Anthropic ships it.

2. **Multi-replica MCP-server safety.** Auth-code + refresh-token stores are still in-memory (process-local). For a 2nd MCP replica we'd need Redis-backed versions (the `pull_state` precedent). One-shot deferred until traffic warrants.

3. **Pre-existing Sessions test flake.** Test-pollution-sensitive. Worth a focused fix.

4. **`openapi.config.json` ref bump.** Reverts to `main` as part of the Phase G merge prep — easy to forget, calling out here.

5. **Tyr dashboard migration** to the new design system — out of scope per spec §11; tracked separately.

6. **MCP rows' metadata shape inconsistency.** The COALESCE fallback works, but a future "harmonize the metadata envelope across all streams" cleanup would be cleaner. Low priority — the fallback is transparent and existing data stays valid.

7. **Mímir** — the next product, separate sprint, separate spec.

---

## Pre-merge checklist (Phase G3+G4)

- [ ] G1 capture-rate measurement landed in this file (above)
- [ ] Result is GREEN, OR Yellow + iteration decision is logged
- [ ] Five sample captured summaries pasted in (verbatim from `telemetry_records WHERE source_api='mcp'`)
- [ ] `openapi.config.json` ref reverted to `main`
- [ ] `vargate-telemetry` merged to `main`
- [ ] `vargate-frontend` merged to `main`
- [ ] Production deploy of merged main verified externally (curl the well-known, the bridge, the MCP /_health)
- [ ] Founder ⌐ to fire when post-merge demo runs cleanly
