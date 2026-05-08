# ADR-002: Vargate Telemetry — Licensing

**Status:** Accepted
**Date:** 2026-05-08
**Deciders:** Founders (technical sign-off)
**Related:** [ADR-001 — System architecture](ADR-001-telemetry-architecture.md)

---

## Context

Vargate Telemetry is a new product line, sister to (but separate from) Vargate
Pro. Pro is Apache-2.0 governance infrastructure for autonomous agents. Pro's
mission is broadest-possible reach — the more environments running governed
agents, the better the world we ship into looks — so a permissive OSI license
fits.

Telemetry is a different posture. It holds customer prompt and response
content. It is sold as a commercial product. Its trust story is operational:
we hold the per-tenant keys, we anchor the audit chain, we run the analyzers
on infrastructure we control. The architecture in ADR-001 doubles down on
that posture (region cells, envelope-encrypted DEKs in HSM, per-tenant
isolation as a first-class concern).

We need a licensing decision **at repo bootstrap**, before any code lands,
because the license is conspicuously displayed on every version of the
Licensed Work. Switching licenses later is messy: contributions made under
one license can't be silently relicensed without contributor consent,
existing distributions stay under the original license, and the perception
hit ("they pulled a Redis / HashiCorp / Elastic") is real.

### Forces at play

- **Procurement friction.** Some enterprise procurement teams have an
  allow-list of acceptable licenses, and "anything OSI-approved" is the most
  common policy. A non-OSI license is a friction point — fixable but real.
- **Hosted-service risk.** A permissive license would let any third party,
  including an AI vendor, fork the Telemetry codebase and ship a competing
  hosted service. Telemetry's pitch is independence from the AI vendor; that
  pitch breaks if the AI vendor can run our code as a service themselves.
- **Trust posture.** The product is bought because Vargate is the trusted
  operator. Source-available is consistent with that ("read the code, audit
  what we do") in a way that pure closed source isn't.
- **Time-bounded restriction.** A four-year sunset to a permissive license is
  short enough to be a credible commitment and long enough to cover the
  commercial window we need.

---

## Decision

**Vargate Telemetry is licensed under the Business Source License 1.1 (BSL
1.1)**, with these parameters:

| Parameter            | Value                                                 |
| -------------------- | ----------------------------------------------------- |
| Licensor             | Twinlite Services Limited                             |
| Licensed Work        | Vargate Telemetry                                     |
| Additional Use Grant | Production use is permitted, except offering the work or a derivative as a hosted/managed service substantially similar to Vargate's commercial offerings (full text in `LICENSE`). |
| Change Date          | 2030-05-08 (four years from first public release)     |
| Change License       | Apache License, Version 2.0                           |

The full canonical BSL 1.1 template, with these parameters substituted, lives
in `LICENSE`. Plain-English answers to the predictable questions live in
`LICENSING-FAQ.md`.

---

## Options considered

### Option A — Apache License 2.0 (matches Pro)

**Pros:** OSI-approved, zero procurement friction, maximally aligned with the
"governance infrastructure should be everywhere" mission of Pro.

**Cons:** Allows any third party, including the AI vendor whose telemetry
this product surfaces, to fork the code and run a competing hosted service.
The product's commercial moat is operational (we hold keys, we anchor the
chain) rather than purely informational, and Apache 2.0 doesn't protect that
moat at all. A well-resourced competitor could ship a hosted clone in a
quarter.

**Why rejected:** The competing-hosted-service risk is not theoretical. The
last five years have seen AWS, Confluent, Elastic, MongoDB, HashiCorp, and
Redis all relitigate this exact question. The pattern is consistent: when a
permissive license meets a hosted-service moat, the permissive license loses,
and the project ends up changing license under duress and with reputational
cost. We'd rather get this right at the start than fix it under fire.

### Option B — Business Source License 1.1 with a four-year Change Date *(chosen)*

**Pros:** Source is published, modifiable, and inspectable. Production use is
permitted for any non-competing purpose (including self-hosting for internal
use). The competing-hosted-service case is restricted, which protects the
commercial moat. The four-year sunset to Apache 2.0 is a credible commitment
that the restriction is time-bounded, not a Trojan horse for closed source.

**Cons:** Not OSI-approved, so projects with strict "OSI only" procurement
policies will need a one-line carve-out. Some open-source purists will
correctly note that BSL is source-available, not open-source — and they're
right. We need a clear FAQ that explains this honestly rather than papering
over it.

**Why chosen:** It fits the product's posture (operational moat, time-bounded
commercial window) and it has been battle-tested by MariaDB, CockroachDB,
Sentry, and others. Procurement friction is real but addressable; the
hosted-service risk is harder to fix once it has shipped.

### Option C — Source-available proprietary (e.g., Sentry's FSL or a custom EULA)

**Pros:** Stronger restrictions on commercial competition; bespoke clauses
possible.

**Cons:** No standard interpretation, every reviewer reads it cold, much
higher procurement friction, and no automatic conversion to a permissive
license. Operating outside an established license family is an ongoing tax
on every legal review.

**Why rejected:** The marginal protection over BSL 1.1 doesn't justify
giving up the standard interpretation that BSL has accumulated. If we needed
something more restrictive than BSL, that would be a signal we should be
fully closed source, not a signal we should invent a fourth license category.

### Option D — Fully closed source, commercial-only

**Pros:** Maximum control of the commercial moat.

**Cons:** Inconsistent with our trust posture. Our pitch — "you can audit how
this works because the code is in the open" — is a non-trivial part of why a
risk-averse buyer chooses us over a closed-source AI-governance vendor.
Throwing that away to chase a moat we can already protect via BSL is bad
trade.

**Why rejected:** The trust posture is the product. A licensing decision that
weakens the trust posture for redundant moat protection is the wrong way
around.

---

## Trade-offs accepted

- **Procurement friction.** Some enterprise customers have OSI-only
  acceptable-license lists. We will need to either get carve-outs or, in some
  cases, lose deals. We accept this. The customers most likely to push on
  this are also the customers most likely to understand BSL once we explain
  it.
- **Two licenses across two repos.** Pro is Apache-2.0; Telemetry is BSL-1.1.
  This means the audit chain primitives shared between Pro and Telemetry
  must live in a separately-licensed package (Apache-2.0) so Telemetry can
  consume them. That extraction is flagged in T2.2 of the sprint plan.
- **CLA on substantial contributions.** To preserve the four-year-to-Apache
  guarantee, we need contributors to grant us the right to relicense their
  contributions at the Change Date. We accept the small friction of a CLA.

---

## Consequences

### What this enables

- A clean trust story: source-available, time-bounded restriction, automatic
  conversion to Apache 2.0.
- A defensible commercial moat for the four-year window during which the
  product is being established.
- A standard, reviewable license that procurement and counsel can interpret
  without reaching for a custom EULA.

### What this complicates

- Cross-repo dependency between Pro (Apache-2.0) and Telemetry (BSL-1.1).
  Solved by extracting shared primitives into a separately-published
  Apache-2.0 package (`vargate-audit-chain`). Flagged in ADR-001 and T2.2.
- Some procurement reviews will need a written explanation of BSL. Solved by
  the public `LICENSING-FAQ.md`, which is intentionally written for non-legal
  buyers.
- Marketing has to be precise: "source-available, time-bounded conversion to
  open source" — not "open source." The wrong words here would be
  open-washing and would be correctly called out.

### What we'll need to revisit

- If procurement friction proves to be a deal-blocker for >20% of pipeline
  by year two, revisit whether to publish a parallel open-core split (a
  permissively-licensed core with BSL extensions). The BSL-only posture is
  the simplest configuration; we revisit only if the data demands it.
- The four-year Change Date is per-version. Each major release ships with
  its own Change Date. We re-confirm at each major release whether four
  years is still right or whether the bar should change.

---

## Action items

1. [x] `LICENSE` file with BSL 1.1 and the four parameters above (this commit).
2. [x] `LICENSING-FAQ.md` published at the repo root (this commit).
3. [x] `.github/LICENSE_HEADER.txt` and `.github/workflows/license-check.yml`
       wired to enforce headers on every new `vargate_telemetry/*.py` file
       (this commit).
4. [x] `pyproject.toml` declares `license = { text = "BSL-1.1" }` (this commit).
5. [ ] Trademark filing for "Vargate" with USPTO (TEAS Plus, classes 9 and
       42). Tracked in `docs/legal/trademark.md`. Not blocking T1.0.
6. [ ] Written IP assignment from founders to Twinlite Services Limited.
       Tracked in `docs/legal/ip-assignment.md`. Must land before T1.1
       starts; without it, the BSL Licensor designation is technically
       defective.
7. [ ] Add a one-line license note to the public site's footer once the v1
       marketing site goes up.

---

## References

- [LICENSE](../../LICENSE) — the binding legal text.
- [LICENSING-FAQ.md](../../LICENSING-FAQ.md) — public-facing answers.
- [ADR-001](ADR-001-telemetry-architecture.md) — system architecture.
- MariaDB BSL 1.1 — https://mariadb.com/bsl11/
- BSL adopters' FAQ — https://mariadb.com/bsl-faq-adopting/
