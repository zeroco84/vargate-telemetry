# Demo data seeding (TM6 T6.S)

`scripts/seed_demo.py` populates a tenant's **Content**, **Sessions**, and
**Usage** dashboards with synthetic data — for a fresh-build demo or a
customer walkthrough so nothing renders empty. The logic lives in
`vargate_telemetry/demo_seed.py` (importable + unit-tested); the script is
the thin CLI.

## What it seeds

| Surface | Records | Demoes |
|---------|---------|--------|
| Content | 3 chats / 7 messages (`compliance_content`) | the content view; **PII redaction** (an email/phone chat + an API-key/SSN chat); a **deleted** chat (tombstone + chain-safe deletion) |
| Sessions | 6 events (`code_analytics` + `compliance_activities`) across 2 actors / 2 days | the Sessions list + source distribution |
| Usage | 3 daily token-usage rows (`admin`) | the Usage table + cache-efficiency panel |

It uses the **real** pipeline — chain-bound `append_telemetry_record` +
AES-GCM `store_content` blobs — so the seeded data decrypts, redacts,
exports, deletes, and **verifies** exactly like production data. After
seeding it asserts `verify_telemetry_chain(...).valid`.

## Running it

Inside the gateway container (it has DB + MinIO + HSM):

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
    exec gateway python scripts/seed_demo.py --tenant-id <TENANT_ID>
```

`--tenant-id` is **REQUIRED and never defaults** — seeding the wrong
tenant pollutes a real audit chain.

> The script ships in the image via the Dockerfile's `COPY scripts/`. If
> you've just added/changed it and haven't rebuilt, run it against a
> source mount instead:
> `docker compose ... run --rm -v "$PWD":/app gateway python scripts/seed_demo.py --tenant-id <ID>`.

## Idempotency / safety

- Every record has a deterministic `demo:` external_id, so re-running
  **adds only what's missing** (content uses dedup-before-store → no
  orphan blobs; blob-less events skip on the dedup UNIQUE).
- It **never deletes chain records** (append-only). There is no `--reset`;
  the demo deletion is done the chain-safe way (a `content_deletion`
  event), so a re-run reports the chat already deleted.
- To retire a *throwaway* demo tenant entirely, delete its rows (never
  the SQLite/Postgres files): `DELETE FROM <table> WHERE tenant_id = '<id>'`
  across `encrypted_secrets`, `tenant_deks`, `telemetry_records`,
  `tenants`. (Only do this for a disposable demo tenant — deleting a real
  tenant's chain records destroys its tamper-evidence.)

## Tests

`tests/test_seed_demo.py` runs the seeders against the isolated `_test`
DB (conftest URL rewrite) and asserts the seeded counts, idempotency
(second run adds nothing), per-surface population, and chain validity.
