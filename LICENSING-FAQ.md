# Vargate Telemetry — Licensing FAQ

This document answers the questions we expect from people evaluating Vargate
Telemetry's licensing posture. The authoritative document is
[`LICENSE`](LICENSE); this FAQ is a plain-English companion.

If you want the short version: **you can run Vargate Telemetry in production
for your own organization, including self-hosting it for internal use.** The
only restriction is offering it to third parties as a hosted or managed
service that competes with our commercial offerings. Four years after each
release, that restriction goes away and the code converts to Apache 2.0
automatically.

---

## Can I use Vargate Telemetry in production?

**Yes.** The Additional Use Grant in the LICENSE explicitly permits production
use, including self-hosting for internal use. Internal company use, customer
deployments, and use as a building block inside a non-competing product are
all permitted.

The one restriction: you cannot offer the Licensed Work, or a derivative of
it, to third parties as a hosted or managed service that provides
functionality substantially similar to Vargate's commercial offerings. In
plain language: you can run it; you just can't run it *for other people* in a
way that competes with us selling it.

If your situation is in a grey area — for example, you're a managed-services
firm running Vargate Telemetry on behalf of a single client under their own
account — write to legal@vargate.ai and we'll talk it through.

## Why isn't this OSI-approved open source?

The Business Source License (BSL) is **source-available**, not OSI-approved
open source. The source is published, you can read it, modify it, and run it.
What it's not is OSI-approved, because it restricts one specific kind of
production use (the competing-hosted-service case).

We chose BSL because Telemetry's value depends on us being a trusted operator
— we hold the keys, we anchor the audit chain, we keep customer prompt and
response content private. A permissive license like Apache 2.0 would let any
third party (including the AI vendor we're providing independence from) fork
the code and ship a competing hosted service overnight, with none of the
operational responsibilities that make the product trustworthy.

This restriction is **time-bounded**: BSL 1.1 converts to Apache License 2.0
on the Change Date (four years from each release) — this version converts on
2030-05-08. The trade-off is "narrow restriction now, full open source later"
rather than "closed source forever."

## Why does Pro use Apache 2.0 while Telemetry uses BSL?

Vargate **Pro** is widely-deployable infrastructure for governing autonomous
agents — a proxy that intercepts agent tool calls, evaluates them against
policy, and writes a hash-chained audit trail. The mission is best served by
the broadest-reach permissive license, because the goal is for governance
infrastructure to be *everywhere*. Apache 2.0 is the right choice.

Vargate **Telemetry** is a different product. It holds customer prompt and
response content. It's sold as a commercial product. The moat is operational
(we hold the keys, anchor the chain, run the analyzers on infrastructure we
control) rather than purely informational. The license reflects that
posture: still source-available, still time-bounded to convert to open
source, but with a narrow restriction during the commercial window.

Two products, two postures, two licenses, one company. Pro is infrastructure;
Telemetry is product.

## What does "substantially similar to Vargate's commercial offerings" mean?

The phrase is intentionally narrow. "Substantially similar" means a hosted or
managed service whose primary function is the same as Vargate Telemetry's
commercial product — i.e., ingesting, analyzing, and reporting on AI usage
data on behalf of paying third-party customers.

It does **not** mean:
- Running Vargate Telemetry inside a larger product where AI-usage telemetry
  is one of many features.
- Embedding components of Vargate Telemetry inside an internal compliance
  pipeline.
- Offering managed services *that include Vargate Telemetry* (e.g., a
  consultancy that deploys and operates Vargate Telemetry on a client's own
  infrastructure under the client's own license).

If you're unsure where your use case sits, write to legal@vargate.ai. We'd
rather have a five-minute conversation than have you guess.

## When does the license convert to Apache 2.0?

For each version of Vargate Telemetry, the BSL terms expire on the **Change
Date** stated in that version's LICENSE file. After the Change Date — or four
years from the first public release of that version, whichever comes first
— the version converts to **Apache License, Version 2.0**.

For the initial version, the Change Date is **2030-05-08**.

The conversion is automatic and irrevocable. We can't take it back.

## Can I contribute to Vargate Telemetry?

Yes. Contributions are welcome under the same BSL terms. Before we accept
substantial code contributions we'll ask you to sign a Contributor License
Agreement (CLA) so we can re-license the contribution at the Change Date
(when the entire codebase converts to Apache 2.0). Small fixes and docs
changes are accepted under the BSL terms without a CLA.

The CLA is the standard mechanism BSL projects use to keep the time-delayed
open-source guarantee sound. It does not transfer ownership; it grants a
license back.

## Does the license cover Vargate's other software?

No. The LICENSE in this repository covers **Vargate Telemetry** only. Vargate
Pro lives in a separate repository under Apache 2.0. Each Vargate product
publishes its own license in its own repository.

## Where can I read more?

- The LICENSE file in this repository — the binding legal text.
- ADR-002 (`docs/adr/ADR-002-licensing.md`) — the decision record explaining
  why we chose BSL 1.1.
- MariaDB's BSL 1.1 page (https://mariadb.com/bsl11/) — the canonical
  template and FAQ for the license itself.
- MariaDB's BSL adopters' FAQ (https://mariadb.com/bsl-faq-adopting/) — the
  general FAQ for projects adopting BSL.

If you have a licensing question this FAQ doesn't answer, email
legal@vargate.ai.
