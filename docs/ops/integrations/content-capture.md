# Content capture — chat message text ingestion (TM5 T5.2)

Once a tenant has onboarded a Compliance Access Key (see
[compliance-access-key.md](compliance-access-key.md)), the content pull
task captures their claude.ai **chat message text** into Ogma's
encrypted store with a hash-chained audit record per message. This is
the most sensitive data Ogma touches; this page is the authoritative
description of what's captured, where it's stored, and how it's
protected.

## Data flow

```
list_organizations          (read:compliance_org_data)
  └─► list_organization_users (read:compliance_user_data) ─► user_ids[]
        └─► list_chats(user_ids[], updated_at>=cursor)
              └─► get_chat_messages(chat_id) ─► chat_messages[]
                    └─► for each NEW message with text:
                          store_content(text)  ─► AES-GCM under tenant DEK ─► MinIO
                          append_telemetry_record(content_ref, content_hash)  ─► hash chain
```

Task: `vargate_telemetry/tasks/pull_compliance.py::_pull_content_for_tenant`
(per-tenant) + `dispatch_compliance_content_pulls` (15-min beat fan-out
to every active tenant; a tenant with no key soft-skips).

## What's captured, and what's stored

| | |
|---|---|
| **Captured** | The **text** content blocks of each chat message (`content[].text`). User + assistant messages. |
| **NOT captured (T5 scope)** | Uploaded files, Claude-generated files, artifacts, projects. A message with no text (e.g. a file-only upload) is skipped, not stored. These are deferred to a later sprint. |
| **Encrypted blob** (MinIO `tenant-content`) | The message text, AES-256-GCM under the per-tenant DEK, AAD-bound to `(tenant_id, content_ref)`. MinIO never sees plaintext. |
| **Telemetry record** (`telemetry_records`) | `source_api='compliance_content'`, `record_type='chat_message'`, `external_id=<message id>`, `content_ref` (MinIO key), `content_hash` (SHA-256 of **plaintext** — survives DEK rotation), `content_size_bytes`, and `metadata` (the searchable envelope: `chat_id`, `role`, `chat_name`, `model`, `project_id`, `organization_uuid`, `user_id`, `user_email`, and `chat_deleted_at` if the chat is soft-deleted). The text itself is **not** in the record — only the encrypted-blob reference. |

The record is chain-bound (`append_telemetry_record`): each content
record extends the tenant's hash chain exactly like usage/activity
records, so content capture is tamper-evident end to end.

## Grain, dedup, and the cursor

- **Per-message grain** (`external_id = message id`). Messages are
  immutable once created, so dedup is clean: re-pulling a chat appends
  only its *new* messages (the existing ones dedup on the
  `(tenant_id, source_api, external_id)` UNIQUE). Dedup is checked
  **before** `store_content`, so a duplicate never writes an orphan
  MinIO blob; the rare check-then-append race deletes the orphan blob
  it wrote.
- **Cursor** (`pull_state`, `source_api='compliance_content'`) is the
  `updated_at` high-water mark. A chat that gains new messages
  re-surfaces because its `updated_at` advances past the cursor, so its
  new messages get captured while the old ones dedup. First run looks
  back `DEFAULT_CONTENT_LOOKBACK_DAYS` (30).
- **Soft-deleted chats** (`deleted_at` set) ARE captured, with the flag
  in metadata. **Hard-deleted** chats never appear in `list_chats`, so
  they're simply absent (not retrievable).
- **Rate budget:** the content stream shares the 600 rpm per-parent-org
  budget with the Activity Feed. `MAX_CHATS_PER_INVOCATION` (200) bounds
  per-tick work; the client's tenacity loop absorbs 429s; the remainder
  rolls forward via the cursor.

## Required scopes (both)

The enumeration chain spans two scopes — the key must carry **both**
`read:compliance_org_data` (list orgs) and `read:compliance_user_data`
(list users + chats). A 403 anywhere in the chain soft-skips the tenant
(`status="no_content_access"`); no sealed key soft-skips with
`status="no_content_key"`. Neither is a Celery retry. See
[compliance-access-key.md](compliance-access-key.md) for the onboarding
that validates both scopes up front.

## Build-blind status (TM5)

Built + unit-tested against the documented contract + mocked Anthropic
responses; the local storage path (encrypt → MinIO → decrypt) is
verified with a real round-trip test. The **live Anthropic pull** is
deferred until a sandbox Compliance Access Key lands (Track-D-D4
pattern). Live close-out: provision a both-scope key → onboard via T5.1
→ trigger a content pull on a real tenant → confirm chats + messages
land encrypted in MinIO + `telemetry_records` and the chain verifies.

## Crypto-shred / deletion

Content inherits the tenant's crypto-shred property: destroying the
wrapped DEK in `tenant_deks` makes every content blob for that tenant
cryptographically inaccessible regardless of whether MinIO still holds
the bytes. Hard-delete endpoints (DSR / eDiscovery deletion) are out of
T5 read-first scope.

## Reference

- Task: `vargate_telemetry/tasks/pull_compliance.py`
  (`_pull_content_for_tenant`, `pull_content_for_tenant`,
  `dispatch_compliance_content_pulls`).
- Client: `vargate_telemetry/anthropic/client.py` (`list_organizations`,
  `list_organization_users`, `list_chats`, `get_chat_messages`);
  factory `compliance_client_for_tenant`.
- Storage: `vargate_telemetry/storage/content.py` (`store_content`,
  `retrieve_content`); chain `vargate_telemetry/chain.py`.
- View (next): T5.3 dashboard "Compliance" content view (read-only).
