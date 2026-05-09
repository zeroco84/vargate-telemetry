import type { Meta, StoryObj } from "@storybook/react";

const meta: Meta = {
  title: "Foundations/Colors",
  tags: ["autodocs"],
  parameters: {
    docs: {
      description: {
        component:
          "Family-color system. **Indigo** is the parent (Vargate.ai). " +
          "**Stamp orange** is reserved for integrity moments — anchor confirmations, " +
          "Telemetry primary CTAs. Never use it on body text or as a fill on more than ~15% of any view.",
      },
    },
  },
};
export default meta;

const Swatch = ({ name, token, hex }: { name: string; token: string; hex: string }) => (
  <div className="vg-card" style={{ overflow: "hidden" }}>
    <div style={{ background: `var(${token})`, height: 80 }} />
    <div className="vg-card__body" style={{ padding: 12 }}>
      <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-sm)", letterSpacing: "var(--ls-wide)" }}>{name}</div>
      <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-3)" }}>{token}</div>
      <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-3)" }}>{hex}</div>
    </div>
  </div>
);

const Group = ({ title, swatches }: { title: string; swatches: { name: string; token: string; hex: string }[] }) => (
  <section style={{ marginBottom: 32 }}>
    <h3 style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", letterSpacing: "var(--ls-widest)", textTransform: "uppercase", color: "var(--color-ink-3)", marginBottom: 12 }}>{title}</h3>
    <div className="vg-grid vg-grid--4">{swatches.map(s => <Swatch key={s.token} {...s} />)}</div>
  </section>
);

export const Palette: StoryObj = {
  render: () => (
    <div>
      <Group title="Indigo · Vargate.ai parent" swatches={[
        { name: "indigo",      token: "--color-indigo",      hex: "#3a3aaf" },
        { name: "indigo-2",    token: "--color-indigo-2",    hex: "#2a2a8e" },
        { name: "indigo-3",    token: "--color-indigo-3",    hex: "#2d2d8a" },
        { name: "indigo-tint", token: "--color-indigo-tint", hex: "#eeedfb" },
      ]} />
      <Group title="Stamp · Telemetry · integrity-only" swatches={[
        { name: "stamp",      token: "--color-stamp",      hex: "#c96442" },
        { name: "stamp-2",    token: "--color-stamp-2",    hex: "#a04d2f" },
        { name: "stamp-3",    token: "--color-stamp-3",    hex: "#7d3a23" },
        { name: "stamp-tint", token: "--color-stamp-tint", hex: "#f7eee8" },
      ]} />
      <Group title="Ink · text + neutrals" swatches={[
        { name: "ink",   token: "--color-ink",   hex: "#1f1f1e" },
        { name: "ink-2", token: "--color-ink-2", hex: "#57534e" },
        { name: "ink-3", token: "--color-ink-3", hex: "#8c8780" },
        { name: "ink-4", token: "--color-ink-4", hex: "#b8b3ac" },
      ]} />
      <Group title="Paper · backgrounds" swatches={[
        { name: "paper",   token: "--color-paper",   hex: "#ffffff" },
        { name: "paper-2", token: "--color-paper-2", hex: "#faf9f7" },
        { name: "paper-3", token: "--color-paper-3", hex: "#f0eee6" },
        { name: "paper-4", token: "--color-paper-4", hex: "#e8e5dc" },
      ]} />
      <Group title="Signal" swatches={[
        { name: "anomaly",       token: "--color-anomaly",       hex: "#b03a2e" },
        { name: "anomaly-tint",  token: "--color-anomaly-tint",  hex: "#f6e6e3" },
        { name: "anchored",      token: "--color-anchored",      hex: "#5d6f5a" },
        { name: "anchored-tint", token: "--color-anchored-tint", hex: "#e8ebe4" },
      ]} />
    </div>
  ),
};
