# OpenAI Admin key — usage + cost onboarding (TM8)

OpenAI is Ogma's first non-Anthropic vendor. A single **OpenAI Admin key** (`sk-admin-…`) unlocks four read-only streams off your OpenAI organization:

| Stream | `source_api` | What it captures |
|---|---|---|
| **Usage** | `openai_admin_usage` | Per-(day, model, user, project, API key) token counts from `/v1/organization/usage/completions` + `/embeddings`. Drives per-user attribution + our cost estimate. |
| **Costs** | `openai_admin_costs` | Authoritative billed spend at project + line-item grain from `/v1/organization/costs` (incl. non-token items like fine-tune training). `bucket_width=1d`. |
| **Audit logs** | `openai_audit_logs` | Admin events (logins, key changes) from `/v1/organization/audit_logs`. |
| **Projects / keys / users** | *(side tables)* | Names for `/usage` rows + the email map that stitches OpenAI activity to the right person. |

This is the **same model as the Anthropic Admin key** (daily aggregates + admin events, no proxies/agents) — Ogma only ever issues `GET`s, so a **read-only** Admin key is sufficient and recommended.

> **Region:** OpenAI has no EU data residency — OpenAI tenants are region `us`.

## Create the key

`platform.openai.com → Settings → Organization → Admin keys → Create`. You must be an **Organization Owner**. Copy the `sk-admin-…` value (shown once). A read-only admin key is enough.

## Connect it in Ogma

Onboarding → the **OpenAI · Admin API** card (or **Settings → Integrations** for an already-onboarded tenant) → paste the key:

1. **Validate** — Ogma probes each endpoint and shows a capability checklist. A bad key or a standard `sk-…` project key (wrong type) returns a clear inline error, never a 500.
2. **Connect** — on a successful probe the key is sealed (encrypted with your per-tenant key; never shown to the dashboard again) and a **90-day backfill** of usage/costs/projects is enqueued. Audit logs are picked up by the hourly poll.

The checklist fields line up 1:1 with the `openai` block of `GET /me/capabilities`:

| Flag | Meaning |
|---|---|
| `admin` | Usage endpoint reachable (the key works). |
| `costs` | Cost endpoint reachable. |
| `audit_logs` | An audit event has actually landed (recent-row presence). Stays `false` below Enterprise — endpoint reachable but returns no events. |
| `project_users` | Your org exposes user-level data. |
| `per_user_breakdown` | `group_by=user_id` returns populated user IDs → **per-user attribution works**. |

> **Tier note (better than Anthropic here):** OpenAI's `per_user_breakdown` populates on **Pay-as-you-go**, not just Enterprise — so per-user attribution works on every paid tier. `audit_logs` only turns true once an audit event lands — below Enterprise the endpoint is reachable but empty, so it stays `false` (audit logging is effectively Enterprise-gated). If your org doesn't expose user IDs, OpenAI activity attributes to the API-key holder or lands in the **Unmapped activity** panel.

## What you'll see after connecting

- **API Usage** (`/api-usage`) — switch the vendor filter to **OpenAI** (or **All** for a cross-vendor view). Costs use OpenAI's published rates; cached input is billed at the ~50% rate (no separate cache-write charge).
- **Users** (`/users`) — OpenAI activity **stitches into the same person as their Claude usage** by email, so one user row spans both vendors.
- **Insights** — cost forecasting, model mix (incl. cross-vendor shift), and project/workspace attribution all include OpenAI.

## What it does NOT capture

- **No ChatGPT web/desktop turn-level capture** — there is no OpenAI equivalent of the Claude MCP connector. Per-user ChatGPT.com activity would need a browser extension (a separate, future surface).
- **No content / transcript text** — OpenAI exposes no Compliance-API-equivalent for chat content. OpenAI capture is **cost + admin events only**. (Claude content capture is the Compliance Access Key path — see `compliance-access-key.md`.)

## Verify / troubleshoot

- `GET /me/capabilities` → `openai.admin: true` immediately on seal (key-presence), before the first pull.
- First usage/cost rows land within ~15 min (usage/costs poll every 15 min; audit/projects hourly).
- Keyless tenants are a no-op — the dispatchers fan out to all tenants and **soft-skip** any without an OpenAI key (no error, no retry).
- A scope-limited key soft-skips the streams it can't read (`status="no_openai_*_access"`); the cursor is untouched so a later key upgrade backfills the window.
