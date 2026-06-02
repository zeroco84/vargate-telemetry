# PII redaction & reveal (TM6 T6.3)

Captured content can contain PII (emails, phone numbers, SSNs, card
numbers, API keys, IPs). Ogma **masks PII by default** wherever content
is shown or exported, and treats *un-masking* as a privileged, audit-
logged action.

## Detection

`pii_detector.detect_and_redact(text)` is **regex-first** (stdlib, no
dependency — ML/NER is explicitly deferred). It detects:

| Type | Example |
|------|---------|
| `email` | `jane@example.com` |
| `phone` | `+1 (415) 555-1234` |
| `ssn` | `123-45-6789` |
| `credit_card` | `4111 1111 1111 1111` |
| `api_key` | `sk-ant-…`, `sk-…`, `AKIA…`, `ghp_…` |
| `ip_address` | `192.168.1.42` |

Matches are replaced with `[redacted:<type>]`. It returns counts per type
(never the matched values) so a redaction summary can be shown or logged
without re-leaking. It errs toward **over-redaction** (a false positive
masks a non-secret; a false negative leaks PII — the former is safer).
Extend it by adding a `(label, pattern)` to `_PATTERNS`.

## Content view

- `GET /content/chats/{chat_id}` returns content with PII **masked**
  (`redacted: true` per message; `redactions` lists the types + counts).
  Any tenant member can view masked content.
- `POST /content/chats/{chat_id}/reveal` (**admin only**) returns the
  same chat **unmasked** (`revealed: true`) and appends a tamper-evident
  `content_reveal` chain event recording who revealed it and when. The
  endpoint builds first, so an unknown / cross-tenant chat 404s *before*
  anything is logged or exposed.

Dashboard: the chat detail shows masked text with a per-message
`🔒 N <type>` note; admins get a **Reveal PII (logged)** button and, once
revealed, an "this reveal was recorded in the audit log" banner.

## Export

- `GET /content/export` redacts by default (`manifest.redacted: true`).
- `GET /content/export?reveal=true` (**admin**) exports full content and
  appends a `content_reveal` event (`scope: export`) first.

The per-message `content_hash` proof (see `content-export.md`) only
matches on a **full** export — masked text won't hash to the plaintext
digest. The chain verification + record existence hold either way.

Dashboard: the export control has an **Include full PII (logged)**
checkbox (admins).

## The audit trail

Every reveal — view or export — is a `content_reveal` record in the
append-only chain (`record_type='content_reveal'`, unique per reveal, so
each exposure is individually attested). These events are inert to the
content view / export queries (filtered out by `record_type`) and don't
affect chain verification.

## Notes / limits

- Regex-first: it will miss exotic formats and over-match some numbers.
  This is the documented TM6 posture; ML/NER detection is deferred.
- `redactions` (counts) are shown to non-admins too — that's metadata
  ("this message contains an SSN"), never the value.
- Tests: `tests/test_pii_detector.py` (per-type detection, counts,
  ordering, passthrough) + `tests/test_content_redaction.py` (view masks
  by default; reveal unmasks + logs; member 403 / unknown 404 log
  nothing; export redacts by default; `reveal=true` is full + logged).
