# Compliance Access Key — content capture onboarding (TM5 T5.1)

Ogma captures three complementary streams of Claude usage:

| Stream | Credential | What it captures |
|---|---|---|
| Usage + members + workspaces | **Admin API key** (`sk-ant-admin01-…`) | Daily token aggregates, org members, workspaces, API keys. |
| Activity Feed | Admin API key (with `read:compliance_activities`) | Event *metadata* — chat-created, file-uploaded, sign-in, admin events. No prompt/response text. |
| **Content capture** | **Compliance Access Key** (`sk-ant-api01-…`) | Chat + **message text** from claude.ai — the actual conversation content, for compliance review. |

This page documents onboarding the **Compliance Access Key**, the
credential that unlocks the content stream. It's a *different key type*
from the Admin API key collected at first onboarding, created in
claude.ai (not the Anthropic Console) by an Enterprise owner.

> **Plan gating:** content capture is **Enterprise-only**. Console / Pro
> / Team organizations can use the Admin key + Activity Feed but cannot
> reach the content endpoints at all (Anthropic returns 403).

## Required scopes — the key needs BOTH

Content capture walks a three-level enumeration chain, and that chain
spans two compliance scopes. When you create the Compliance Access Key
in claude.ai, grant it **both**:

| Scope | Needed for |
|---|---|
| `read:compliance_org_data` | `GET /v1/compliance/organizations` — enumerate orgs to get each `org_uuid`. |
| `read:compliance_user_data` | `GET /v1/compliance/organizations/{org_uuid}/users` (to get `user_ids`) **and** `GET /v1/compliance/apps/chats?user_ids[]=…` (the content itself). |

The enumeration chain is: **organizations → users → chats → messages.**
A key with only one scope passes the first probe and then fails at
content-pull time, so onboarding validates **both** up front (see below)
and rejects a half-scoped key with a `code: insufficient_scope` error
naming the missing scope.

## How an admin onboards the key

In the dashboard: **Settings → Integrations → Compliance Access Key →
paste the key → Connect**. The card is **admin-only**; a non-admin sees
"An admin can connect a Compliance Access Key to enable content
capture." (The backend enforces the gate independently — `require_admin`
on `POST /onboarding/compliance-key`.)

What happens on submit (`POST /onboarding/compliance-key`):

1. **Format guard** (local, no network). An Admin key
   (`sk-ant-admin01-…`) is rejected with a pointed message; anything
   that isn't `sk-ant-api01-…` is rejected as malformed.
2. **Live probe** — confirms the key actually reaches content:
   - `GET /v1/compliance/organizations` (limit 1) — confirms the key
     works + `read:compliance_org_data` + Enterprise plan, and yields an
     `org_uuid`.
   - `GET /v1/compliance/organizations/{org_uuid}/users` (limit 1) —
     confirms `read:compliance_user_data`, the scope content needs.
   - The Activity Feed is deliberately **not** used as the validator —
     an Admin key can reach it too, so a 200 there wouldn't prove the
     key is a content-capable Compliance Access Key.
3. **Seal** — on success the key is AES-GCM-encrypted under the tenant
   DEK and UPSERTed into `encrypted_secrets` under the name
   `anthropic_compliance_access_key` (re-submitting **rotates** the key
   in place). A bad / half-scoped key 400s *before* this step, so there
   is no partial state.

Errors surface with a `code` the UI branches on: `wrong_key_type`,
`malformed_compliance_key`, `invalid_compliance_key`,
`insufficient_scope`, `admin_required` (403), `anthropic_rate_limited`
(503).

## The capability signal

`GET /me/capabilities` returns a `content_capture` boolean. Unlike the
other four capability bools (which reflect whether telemetry rows of a
given `source_api` exist), `content_capture` reflects **whether a
Compliance Access Key is sealed** for the tenant — the capability is
unlocked by *holding the key*, not by content having been pulled yet.
The Settings card flips to "Connected" the moment the key is sealed.

Content pulling itself (chats → messages → encrypted storage) is the
**T5.2** content-pull task; this page covers only the key onboarding
that unblocks it.

## Where the key lives

- **Stored:** `encrypted_secrets`, AES-GCM under the per-tenant DEK,
  AAD-bound to `(tenant_id, "anthropic_compliance_access_key")` — same
  seal path as the Admin key. RLS-isolated per tenant.
- **Never** logged, never returned by any read endpoint, never exposed
  to the agent. The execution path unseals it per-operation and drops
  the plaintext when the client closes.

## Build-blind status (TM5)

T5.1 was built **blind**: there is no sandbox Compliance Access Key from
Anthropic yet (no timeline). The validation + seal logic is built and
unit-tested against the documented API contract (reconned 2026-06-02)
with mocked client responses — the live probe path runs only against
stubs until a key lands. Same pattern as Track D's D4 (wire key → live
backfill), with a deferred live-verify checklist:

1. Provision a Compliance Access Key on an Enterprise claude.ai org,
   with **both** `read:compliance_org_data` and
   `read:compliance_user_data` scopes.
2. Submit it via **Settings → Integrations → Compliance Access Key**;
   confirm it validates + seals and `content_capture` flips to true.
3. (T5.2) Trigger a content pull; confirm chats + messages land
   encrypted in MinIO + `telemetry_records`.

## Reference

- Endpoint: `POST /onboarding/compliance-key` (`operationId:
  submitComplianceKey`) — see `openapi/ogma-api.yaml`.
- Backend: `vargate_telemetry/api/compliance_key.py`,
  `vargate_telemetry/anthropic/client.py` (`list_organizations`,
  `list_organization_users`), `vargate_telemetry/anthropic/factory.py`
  (`ANTHROPIC_COMPLIANCE_KEY_SECRET`).
- Frontend: `apps/ogma-dashboard/src/pages/settings/Settings.tsx`
  (`ComplianceAccessKeyCard`), `src/lib/onboarding.ts`
  (`submitComplianceKey`).
- Anthropic docs:
  https://platform.claude.com/docs/en/manage-claude/compliance-org-data
