# Budget alert channels — email, Slack, PagerDuty (TM5 T5.4)

A budget fires an alert when current-period spend crosses a threshold
(70% / 85% / 100% of the cap). Each budget configures **who** gets
notified, per channel. Email is the default; Slack and PagerDuty fire
only when configured.

## Per-channel config

A budget's `alert_recipients` is a JSONB object:

```json
{
  "email": ["finance@acme.com", "ops@acme.com"],
  "slack_webhook": ["https://hooks.slack.com/services/T.../B.../..."],
  "pagerduty_key": ["<32-char Events API v2 routing key>"]
}
```

Set it in the dashboard: **Budgets → New budget** (or edit) → the
Email / Slack / PagerDuty fields (comma- or space-separated). Any channel
can be empty. Validation rejects malformed values up front (422):
non-email addresses, Slack URLs that aren't `https://hooks.slack.com/…`,
empty/whitespace PagerDuty keys, and unknown channel keys.

| Channel | What's sent | How to get the credential |
|---|---|---|
| **Email** (default) | The branded HTML budget-alert email (via AWS SES). | Just an address; SES sender identity is configured ops-side (see `aws-ses.md`). |
| **Slack** | A Block Kit message to the incoming webhook. | In Slack: *Apps → Incoming Webhooks → Add to a channel* → copy the `https://hooks.slack.com/services/…` URL. The URL **is** the secret. |
| **PagerDuty** | An Events API v2 `trigger` event (severity `critical` at 100%, else `warning`; stable `dedup_key` per crossing). | In PagerDuty: a service → *Integrations → Events API v2* → copy the Integration (routing) Key. |

## Delivery semantics

- **Best-effort + isolated.** `notify.send_budget_alert` dispatches over
  every configured channel; each channel's failure is caught, logged,
  and recorded in a per-channel summary — **never raised**. A failing
  Slack webhook does not block email or PagerDuty, and a notify failure
  **never rolls back the alert-event row** (the dashboard `/alerts` view
  is the source of truth; rolling back would un-dedup and re-fire the
  alert every 15-minute tick).
- **Dedup.** The evaluator records each (budget, period, threshold)
  crossing once (`budget_alert_events` UNIQUE), so each channel gets
  exactly one notification per crossing. PagerDuty additionally carries
  a stable `dedup_key` so a retry collapses into one incident.
- **Rate / timeout.** Slack + PagerDuty POSTs use a 10s timeout. Webhook
  URLs and routing keys are **redacted in logs** (the URL/key is a
  secret).

## Secrets handling

Slack webhook URLs and PagerDuty routing keys are stored in the budget
row (`alert_recipients` JSONB) like any other budget config — they are
**not** rendered back in the dashboard detail view (it shows email
addresses, but only *counts* for Slack/PagerDuty) and are redacted in
logs. They are not HSM-sealed (unlike the Anthropic keys) — they're
per-budget delivery targets, not tenant-wide credentials; revoke by
rotating the webhook/key in Slack/PagerDuty.

## Migration

`alert_recipients` was a `varchar[]` of emails through TM4. Migration
`0023_budget_alert_channels` converts it in place to the per-channel
JSONB, wrapping every existing email list under the `email` key — no
recipient is lost and email keeps firing unchanged. Downgrade extracts
the `email` array back to `varchar[]` (Slack/PagerDuty recipients are
dropped, lossy by design).

## Reference

- `vargate_telemetry/notify/budget_alert.py` (`send_budget_alert` dispatch),
  `notify/slack.py`, `notify/pagerduty.py`, `notify/email.py`.
- `vargate_telemetry/api/budgets.py` (`AlertRecipients` model + validation).
- `vargate_telemetry/tasks/evaluate_budgets.py` (the evaluator that calls it).
- Frontend: `apps/ogma-dashboard/src/pages/dashboard/Budgets.tsx` (create
  form), `BudgetDetail.tsx` (per-channel summary), `lib/budgets.ts`.
