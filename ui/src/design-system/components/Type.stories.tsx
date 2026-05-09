import type { Meta, StoryObj } from "@storybook/react";

const meta: Meta = {
  title: "Foundations/Type",
  tags: ["autodocs"],
};
export default meta;

const Sample = ({ size, weight, ls, label, family = "sans" }: { size: string; weight: number; ls: string; label: string; family?: "sans" | "mono" }) => (
  <div style={{ display: "grid", gridTemplateColumns: "180px 1fr", gap: 24, padding: "16px 0", borderBottom: "1px solid var(--color-line)" }}>
    <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-3)", letterSpacing: "var(--ls-wide)" }}>
      {label}<br />{size} · {weight} · ls {ls}
    </div>
    <div style={{ fontFamily: family === "mono" ? "var(--font-mono)" : "var(--font-sans)", fontSize: `var(--fs-${size})`, fontWeight: weight, letterSpacing: `var(--ls-${ls})` }}>
      {family === "mono"
        ? "0xc93a4d12b21188e1 · 2026-05-08T14:32:11Z"
        : "Independent audit of AI usage."}
    </div>
  </div>
);

export const Scale: StoryObj = {
  render: () => (
    <div>
      <Sample label="display"  size="4xl"  weight={500} ls="tightest" />
      <Sample label="h1"       size="3xl"  weight={500} ls="tighter" />
      <Sample label="h2"       size="2xl"  weight={500} ls="tighter" />
      <Sample label="h3"       size="xl"   weight={500} ls="tight" />
      <Sample label="lead"     size="lg"   weight={400} ls="tight" />
      <Sample label="body"     size="base" weight={400} ls="normal" />
      <Sample label="small"    size="sm"   weight={400} ls="normal" />
      <Sample label="mono / hash" size="sm"  weight={400} ls="wide"  family="mono" />
      <Sample label="mono / label" size="xs" weight={500} ls="wider" family="mono" />
    </div>
  ),
};
