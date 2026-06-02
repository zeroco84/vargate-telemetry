# eDiscovery content export (TM6 T6.2)

`GET /content/export` (admin only) builds a downloadable **ZIP bundle**
of a tenant's captured content for legal / compliance discovery. Its
differentiator over a plain dump is a **chain-verification proof**: an
auditor can independently confirm the exported content is exactly what
was recorded, and that the underlying audit chain is intact.

## The bundle

```
vargate-export-{tenant}-{YYYYMMDDTHHMMSSZ}.zip
├── manifest.json     export metadata, scope, counts, chain-verification summary
├── chats.json        the captured chats + decrypted message text
├── chain_proof.json  per-record hash-chain proof
└── README.txt        how to verify (human-readable)
```

- **`manifest.json`** — product/export type, `generated_at`, the scope
  filters used, counts (chats / messages / purged), and the
  `chain_verification` summary (`valid`, `record_count`, and — on
  failure — `failure_reason` + `failed_at_index`).
- **`chats.json`** — one entry per chat (name, model, user) with its
  messages: role, timestamp, decrypted `content`, and a `purged` flag.
- **`chain_proof.json`** — per record: `external_id`, `chat_id`,
  `chain_seq`, `chain_self_hash`, `chain_prev_hash`, `content_hash` (all
  hex), `occurred_at`, `purged` — plus the overall `verification` result.

## Scope

Always scoped to the calling tenant (RLS). Optional query params narrow
it further:

| Param | Meaning |
|-------|---------|
| `subject_user_id` | only one data subject's content (pairs with a DSR export) |
| `start` | only records at/after this timestamp (ISO-8601) |
| `end` | only records *before* this timestamp (exclusive) |

```bash
GET /content/export?subject_user_id=<id>&start=2026-01-01T00:00:00Z
```

## How an auditor verifies it

1. **Chain intact** — `chain_proof.json → verification.valid == true`
   means the tenant's whole append-only hash chain (GENESIS → tip) is
   internally consistent: no record inserted, removed, or modified.
2. **Content unaltered** — for any message in `chats.json`, take its
   text, UTF-8 encode, SHA-256 it, and match the digest to that record's
   `content_hash` in `chain_proof.json` (same `external_id`). A match
   proves the exported text is exactly what was chained.

`content_hash` is the SHA-256 of the *plaintext* (stored in the clear on
the record), so it is recomputable from the export and survives DEK
rotation / blob deletion.

## Purged content (T6.1 interplay)

A message deleted via T6.1 is **still in the proof** — its chain record +
`content_hash` remain (proving it existed and was deleted) — but its
`content` is `null` and `purged` is `true`. Crypto-shredded tenants
export with every message purged + the chain still verifying.

## From the dashboard

**Content** → **Export bundle (zip)** (admins only) downloads the bundle
for the whole tenant. Per-subject / date-scoped exports are issued via
the API today.

## Notes / limits

- Synchronous + in-memory for now; a very large export is a future
  async/Celery follow-up.
- The bundle ships the exported records' chain fields + our
  `verify_telemetry_chain` result. The per-record `content_hash` check is
  fully independent; a full independent re-walk of the entire chain needs
  the live system (or a future full-chain export option).
- Tests: `tests/test_content_export.py` (bundle structure, the
  plaintext↔content_hash proof, scope filters, purged-in-proof, admin
  gating).
