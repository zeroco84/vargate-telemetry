# Vargate Telemetry — Design System Handoff

This package is the source of truth for Vargate Telemetry's UI surface. It contains design tokens, primitives (Button, Input, Card), data components (Table, KpiTile, AlertRow), product-distinctive components (RedactionToggle, DrillThrough), application chrome (Sidebar, Topbar), and state placeholders (Empty / Loading / Error). Each component ships with a Storybook story.

## Direction

**Ledger.** Grotesk-led, ink-on-paper, instrument-grade. Built for the CISO / compliance / CFO buyer. Calm, defensible, tamper-evident — never playful, never AI-magical.

Reference brands: Stripe (trust), Linear (clarity), Vanta (compliance posture). Never derivative of any single one.

## Family architecture

| Brand | Identity color | Token |
| --- | --- | --- |
| **Vargate.ai** (parent) | indigo `#3a3aaf` | `--color-indigo` |
| **Vargate Telemetry** (this product) | warm orange `#c96442` | `--color-stamp` |
| **Vargate Pro** (sibling) | ink `#1f1f1e` | `--color-pro` |

All three share the same audit trail, neutrals, type scale, and components. Only the accent and lockup label change.

## Color rules

- **Stamp orange** is reserved for integrity moments — anchor confirmations, audit stamps, Telemetry primary CTAs. Never on body text. Never on more than ~15% of any view.
- **Anomaly red** (`--color-anomaly`) only on detection events; never neutral state.
- **Anchored green** (`--color-anchored`) only on confirmed-on-chain status; pending uses `--color-ink-3` on `--color-paper-3`.
- Backgrounds are warm — `--color-paper`, `--color-paper-2`, `--color-paper-3`. No cool neutrals.

## Type

- **Inter Tight** for UI, headlines, body. Weights 400 / 500 / 600. Negative tracking on display sizes.
- **JetBrains Mono** for every hash, ID, timestamp, audit field, and label. Tabular numerics on by default.

## Getting started

```bash
pnpm install
pnpm storybook
```

Storybook runs at `http://localhost:6006` and is the canonical review surface.

## File layout

```
ui/
├─ .storybook/                Storybook config
├─ src/design-system/
│  ├─ tokens.json             Authoritative token source
│  ├─ tokens.css              Generated CSS custom properties — do not edit by hand
│  ├─ tokens.ts               TS export of the same tokens for runtime use
│  ├─ index.ts                Public package surface
│  └─ components/
│     ├─ styles.css           Class-based styles for every component
│     ├─ icons.tsx            16×16 stroke icon set
│     ├─ Button.tsx           Primitives
│     ├─ Input.tsx
│     ├─ Card.tsx
│     ├─ Table.tsx            Data viz
│     ├─ KpiTile.tsx
│     ├─ AlertRow.tsx
│     ├─ RedactionToggle.tsx  Distinctive — three-step audited reveal
│     ├─ DrillThrough.tsx     Distinctive — right-side detail slide-over
│     ├─ Sidebar.tsx          Chrome
│     ├─ Topbar.tsx
│     ├─ States.tsx           Empty / Error / Loading
│     └─ *.stories.tsx        One story per component
├─ package.json
├─ tsconfig.json
└─ README.md
```

## Component conventions

- **No CSS-in-JS.** Components attach class names from `components/styles.css`. The whole stylesheet is human-readable and direct-editable.
- **No fetching inside components.** Data tables, alert rows, and tiles take rows + values as props. Hosts own loading and error logic — pair with `<LoadingState>` / `<ErrorState>` primitives.
- **Sort and selection are controlled.** `<Table>` accepts `sort` + `onSortChange` and does not reorder rows internally. The host owns query state.
- **Tabular numerics on by default.** Audit work depends on column-aligned digits. `font-variant-numeric: tabular-nums` is set globally on `table` and `.vg-tabular`.

## Distinctive components

### `<RedactionToggle>`

Three-step interaction for any sensitive value: **locked → confirming → revealed**. Revealing is itself an audited event. Pair with a `receipt` prop (typically the row hash + actor) so the user sees, after the seal breaks, exactly what was written to the audit trail.

Do **not** use this as a generic show-password toggle. The cost of looking is the point.

### `<DrillThrough>`

Right-side slide-over for inspecting one row from a table without losing your place. Mandatory crumbs at the top so users always know which entity they are looking at. Closes on Esc and on scrim click.

## Acceptance checklist for new components

- [ ] Tokens used via CSS custom properties; no hex codes in component files.
- [ ] Mono font on every hash, ID, timestamp, label.
- [ ] Stamp orange used only for integrity actions, never as a fill on >15% of the surface.
- [ ] Storybook story exists and demonstrates the empty + populated states.
- [ ] Screen-reader pass: every interactive element has an accessible name; expanded/active state exposed via `aria-*`.
- [ ] No drop shadows softer than `--sh` and no border radii larger than `--r-lg` (5px / 8px).

## What this package is not

- **Not a marketing component library.** Marketing surfaces (the seal mark, hero compositions, gradients) live separately and are allowed liberties this product UI is not.
- **Not a Vargate.ai parent system.** This is the Telemetry product UI. The parent system shares tokens but ships separately.

## Owners

Design: design@vargate.ai · Engineering: ui@vargate.ai
