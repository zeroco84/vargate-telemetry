# AWS SES — outbound email integration

Ogma uses AWS SES for all transactional outbound email today. The
first consumer is budget-alert notifications (TM3); future channels
(digest summaries, expiring-credential warnings) will share the
same boto3 wrapper at `vargate_telemetry/notify/email.py`.

## Sender identity

| Setting | Value |
|---|---|
| Verified domain | `vargate.ai` (DKIM + SPF set; verified at the Twinlite organization level for the other Twinlite projects) |
| From address | `alerts@vargate.ai` (verified email identity in the EU-West-1 region) |
| Reply-to | `noreply@vargate.ai` — replies aren't monitored; surfaced in the email footer along with the dashboard link |

If we ever need a subdomain sender (e.g. `alerts@ogma.vargate.ai`):
verify it as a separate identity, add SPF + DKIM CNAMEs to the
Cloudflare DNS for `ogma.vargate.ai`. **Do not** swap
`OGMA_ALERT_FROM_ADDRESS` to the new identity until SES shows the
identity as `Verification status: Verified`.

## SES region

`eu-west-1`. Picked to match the primary EU tenant region. SES is
regional and the sender identity must be verified in the same
region the boto3 client connects to.

If we ever launch a US-only tenant cluster and want US-East mail
egress for latency reasons, verify the same domain in US-East-1
separately and key the env var per region. Today: single region,
single sender identity.

## Required environment variables

| Var | Required by | Notes |
|---|---|---|
| `AWS_SES_REGION` | `notify/email.py` | Defaults to `eu-west-1` if unset; explicit-set in prod. |
| `OGMA_ALERT_FROM_ADDRESS` | `notify/email.py` | Must match a verified SES identity (email or domain). The wrapper raises `SesNotConfigured` if unset; the alert evaluator catches and logs, but no email goes out. |
| `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` | boto3 default credential chain | Pick whichever credential surface the prod box uses (an IAM-user keypair today; will migrate to an EC2 IAM role when the box moves to a managed-credential host). Both are auto-discovered by boto3 — don't read them in our code. |
| `OGMA_DASHBOARD_URL` | `notify/budget_alert.py` | The "View & acknowledge in dashboard" CTA URL. Defaults to `https://ogma.vargate.ai`. Set per env in dev/staging if you want clickable links from local SES sandboxes. |

## IAM policy

The credential used by the gateway / worker needs `ses:SendEmail`
on the verified identity ARN. Reuse the existing Twinlite SES
sender policy (or attach the inline policy below).

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "ses:SendEmail",
      "Resource": "arn:aws:ses:eu-west-1:<account>:identity/vargate.ai"
    }
  ]
}
```

Scope to the domain identity, not `*`. If a future channel needs
SendRawEmail (attachments) or SendTemplatedEmail (SES templates),
broaden the action list at that time — don't pre-grant.

## Sandbox status

AWS accounts start SES in **sandbox mode**, which restricts:

- Senders: must be verified
- Recipients: must be verified
- Volume: 200 emails/24h, 1/second

Production access (no recipient restriction, 50K/day default
quota) is requested via the SES console → "Request production
access". Approval is usually within 24h. **Check sandbox status
before assuming an alert email lands** — a `MessageRejected: Email
address is not verified` in CloudWatch is the sandbox-restriction
signal.

To check status: AWS Console → SES → Account dashboard → "Sending
statistics". If sandbox: the box at the top reads "Your Amazon SES
account is in the sandbox in EU (Ireland)".

## Failure modes the wrapper handles

| Failure | Outcome | Caller behaviour |
|---|---|---|
| `OGMA_ALERT_FROM_ADDRESS` unset | `SesNotConfigured` raised by `send_email` | Alert evaluator logs `WARNING send_budget_alert: SES not configured; alert recorded but email NOT sent`. The alert event row stays in the DB; the dashboard surfaces it. |
| SES rejects (unverified sender, sandbox recipient, etc.) | `EmailDeliveryError` raised | Alert evaluator logs `ERROR` with the underlying SES message. Alert row remains; no rollback. |
| Recipient list empty | `send_budget_alert` no-ops | Budget without alert recipients is a valid configuration — customer may be iterating toward one. No log noise. |
| Network timeout | boto3 retries once (config), then surfaces as `EmailDeliveryError` | Same as the SES-rejects path. |

## Local development

If you don't want to hit real SES locally:

1. **Skip the env var** — `OGMA_ALERT_FROM_ADDRESS` unset → the
   `SesNotConfigured` branch fires, the alert row is still
   recorded, and the dashboard shows it. Useful when developing
   the dashboard's alert acknowledgment flow.
2. **Use a test credential against the SES sandbox** — verify
   your own email as a recipient + sender, set the env vars,
   trigger an alert against your own email address. Live
   round-trip without touching production traffic.

## CloudWatch + monitoring

SES auto-publishes to CloudWatch under `AWS/SES`:

- `Send` — accepted by SES
- `Bounce` — recipient rejected (hard or soft)
- `Complaint` — recipient hit "spam" in their client
- `Reject` — SES refused (most common: sandbox restriction)

The boto3 wrapper logs `INFO send_email: SES accepted MessageId=…`
on success — searching CloudWatch by `MessageId` gives the full
delivery history. Bounces / complaints on `alerts@vargate.ai` are
sent to SES's default bounce-handling SNS topic (or to the
verified-domain owner's mailbox if no SNS topic is configured).

If we ever see a customer report "I never got the alert email":
check CloudWatch's `Bounce` metric for that day, and if a bounce
fired, the SES → SNS feed (or the bounce-handling mailbox) will
have the SMTP-level error message.

## Cost

SES outbound is $0.10 per 1,000 emails for the first 62,000 from
an EC2-hosted sender; we're nowhere near that. Budget alerts are
~3 emails per (budget, period) maximum (one per threshold), so a
tenant with 5 budgets crosses ~15 alerts/month worst case. Cost is
not a constraint today — order of cents per month per tenant. No
need for batching or `SendBulkEmail` until the volume picture
changes.

## Where to look when something's wrong

1. `docker compose logs gateway celery-worker celery-beat | grep send_email`
   on the prod box — the wrapper logs both the `INFO accepted` and
   any `ERROR` from boto3.
2. AWS Console → CloudWatch → Metrics → `AWS/SES` for the regional
   delivery stats.
3. AWS Console → SES → Sending statistics — sandbox banner + 24h
   counters.
4. AWS Console → SES → Verified identities — confirm
   `alerts@vargate.ai` (or whichever identity is set) shows
   "Verified".
