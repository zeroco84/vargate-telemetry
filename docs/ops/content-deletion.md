# Content deletion, DSR & offboarding (TM6 T6.1)

Ogma captures chat + message content (when a Compliance Access Key is
configured — see `integrations/content-capture.md`). Compliance regimes
(GDPR/CCPA right-to-erasure, retention policy, account offboarding)
require that captured content can be **deleted on request** — while the
tamper-evident audit chain stays intact.

Ogma deletes content **chain-safely**: it makes the content unreadable
and records the deletion as a new, append-only chain event. It **never**
mutates or removes a chain record — that would break tamper-evidence.
This is the AGCS posture: *prove the content existed, and prove it was
deleted.* `verify_telemetry_chain` stays green before and after every
deletion.

## The three scopes

| Scope | What it deletes | How | Endpoint |
|-------|-----------------|-----|----------|
| **Per-chat** | One chat's message content | delete the blobs | `DELETE /content/chats/{chat_id}` |
| **Per-user (DSR)** | All of one data subject's content, across every chat | delete the blobs | `DELETE /content/users/{subject_user_id}` |
| **Per-tenant (offboarding)** | Everything for the tenant — all content **and** sealed keys | crypto-shred the tenant DEK | `POST /content/tenant/shred` |

All three are **admin-only** and require a **reason** (recorded in the
audit trail). Per-chat and per-user deletes are **idempotent** —
re-running reports `already_deleted` and never duplicates an event.

### Per-chat / per-user
Each matching message blob is deleted from object storage, then a
`content_deletion` chain event is appended **per message** (carrying the
deleted record's `external_id`, `chat_id`, scope, reason, `requested_by`,
and — for a DSR — the `dsr_subject`). The original `chat_message` record
remains in the chain; its `content_ref` now dangles, so reads return no
content. Order is **blob-delete first, event second** — a crash between
is recoverable (re-run is idempotent) and can never leave the chain
claiming "deleted" while the content is still readable.

### Per-tenant crypto-shred (terminal)
Offboarding destroys the tenant's wrapped DEK (`tenant_deks` row). Every
content blob **and** every sealed secret (admin/compliance keys) for the
tenant becomes permanently undecryptable at once — the bytes may remain
in object storage but cannot be read. One tenant-scoped `content_deletion`
event is appended. This is **TERMINAL and irreversible.**

To guard against a fat-finger, the request must include
`confirm_tenant_id` equal to the caller's own tenant_id, or it is
rejected (`400 confirm_mismatch`).

```jsonc
// POST /content/tenant/shred
{ "reason": "Account closed — contract #4471", "confirm_tenant_id": "tnt_eu_…" }
```

## From the dashboard

1. **Content** → open a chat.
2. Admins see **Delete chat content**. Click it, enter a reason
   (e.g. *"DSR erasure request #1234"*), and confirm.
3. The chat re-renders as a **tombstone**: a "Content purged" banner with
   the date + reason, and each message shows `[content deleted]`. In the
   Content list the chat carries a **purged** badge.

Per-user DSR and per-tenant offboarding are issued via the API today
(admin-only); the dashboard surfaces per-chat deletion + the tombstones.

A chat is shown **fully purged** only when *every* one of its messages
has been deleted (or the tenant was shredded) — a per-user DSR that
touches only some messages of a multi-subject chat leaves the chat
partially purged (the deleted messages tombstone individually; the chat
itself is not flagged purged). The list and detail views agree on this.

## What survives a deletion

- **The chain records** — immutable. `chain_seq`, `prev_hash`,
  `self_hash`, and `content_hash` are untouched. `content_hash` is the
  SHA-256 of the *plaintext*, stored in the clear, so it survives blob
  loss and DEK destruction — the chain still verifies, and you can still
  *prove what content existed*.
- **The `content_deletion` events** — the append-only, tamper-evident
  record of who deleted what, when, and why.

What does **not** survive: the readable content itself (blob deleted, or
DEK shredded).

## Verifying

```bash
# Chain stays valid across deletions:
GET /chain/verify        # → { "valid": true, ... }

# Deletion events are queryable (record_type = 'content_deletion').
```

Backend tests: `tests/test_content_deletion.py` (service + endpoints +
view tombstones, against real blobs + a real chain). The deletion arc
also passed a four-dimension adversarial review (chain-safety, RLS,
content-unreadability, correctness) before merge.
