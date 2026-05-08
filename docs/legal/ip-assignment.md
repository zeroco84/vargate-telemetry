# IP assignment — Founders → Twinlite Services Limited

**Status:** Not started — to be signed by founders before T1.1 begins.
**Owner:** Founders (legal admin).
**Date opened:** 2026-05-08
**Blocking:** T1.1 and beyond.

This is a **paperwork-only** tracking document. No code lives here. The
assignment happens outside this repo; this file records the state so the
team knows it's in flight and the BSL Licensor designation in `LICENSE`
("Twinlite Services Limited") is backed by an actual chain of title.

---

## Why this matters

The `LICENSE` file names **Twinlite Services Limited** as the BSL Licensor.
For that designation to be legally sound, Twinlite Services Limited must
**own** the copyright in the Licensed Work. By default, code written by
individual founders before any assignment belongs to the individuals, not
the entity.

Without a written IP assignment from each founder to Twinlite Services
Limited:

- The BSL license grant is technically **defective** — the entity can't
  license what it doesn't own.
- A later dispute between founders, or between a founder and the company,
  could surface a claim that some portion of the Licensed Work was never
  Twinlite's to license.
- Customers conducting source-of-IP diligence will flag this. Enterprise
  procurement increasingly asks for IP-assignment evidence as part of
  vendor onboarding.

The fix is a one-time, short signed document. Until it's signed, work on
T1.0 (repo bootstrap, scaffolding) can proceed because there is no
substantial code to assign yet. Once T1.1 starts producing code, every
unassigned commit compounds the problem.

---

## What we need

A signed agreement from **each individual founder** to **Twinlite Services
Limited** that covers:

- Assignment of all right, title, and interest in any code, documentation,
  designs, or other materials authored by the founder relating to Vargate
  Telemetry, Vargate Pro, and any Vargate-related products, whether
  pre-existing or prospective.
- Carve-outs for any clearly pre-existing personal projects unrelated to
  Vargate (founders should list these as a schedule).
- Effective date that pre-dates the first Vargate-related commit.
- Confirmation that the assignment is irrevocable.
- Standard further-assurances clause (founder will sign whatever else is
  needed to perfect the assignment).

A standard "Founder IP Assignment Agreement" template covers all of this.
Most startup-friendly law firms have a template; alternatively, the
[Stripe Atlas template library](https://stripe.com/atlas/guides) and the
[Y Combinator legal forms](https://www.ycombinator.com/legal) both publish
boilerplate that is fit for purpose.

---

## Process

1. Founders engage counsel (or use a vetted template) to draft the
   agreement.
2. Each founder signs.
3. Twinlite Services Limited counter-signs.
4. The signed PDF is stored in the company's records (NOT in this repo —
   never commit signed legal documents to a public repo).
5. The reference below is updated with the document name and signing date.

---

## Tracking

| Founder        | Drafted | Signed by founder | Counter-signed | Filed in records |
| -------------- | ------- | ----------------- | -------------- | ---------------- |
| TBD            | —       | —                 | —              | —                |
| TBD            | —       | —                 | —              | —                |

When all rows are complete, this document becomes a historical record. The
signed PDF is the authoritative artifact and lives in the company's
document store, not in this repo.

---

## Until then

Work on T1.0 (this commit) can proceed because the only code in the repo is
licensing scaffolding, README content, and CI/config — there is no
substantial product code to assign.

**T1.1 (the first product code) must not start until this assignment is
signed.** If T1.1 is starting and this document still shows blank rows,
**stop and resolve before producing code.**
