import type { Meta, StoryObj } from "@storybook/react";
import { KpiTile } from "./KpiTile";

const meta: Meta<typeof KpiTile> = {
  title: "Data/KpiTile",
  component: KpiTile,
  tags: ["autodocs"],
};
export default meta;
type S = StoryObj<typeof KpiTile>;

const trend = [42, 44, 39, 51, 48, 55, 60, 58, 62, 70, 68, 74];

export const Default: S = {
  args: {
    label: "Events ingested · 24h",
    value: "1,284,503",
    delta: "+8.4% vs prior 24h",
    tone: "up",
    spark: trend,
  },
};

export const Anomaly: S = {
  args: {
    label: "Anomalies open",
    value: "3",
    delta: "+2 since 09:00",
    tone: "down",
    spark: [0, 0, 1, 1, 0, 1, 2, 2, 1, 3],
    sparkColor: "var(--color-anomaly)",
  },
};

export const Stamp: S = {
  args: {
    label: "Last anchor",
    value: "00:14:22 ago",
    delta: "Block 19,482,011",
    tone: "warn",
    sparkColor: "var(--color-stamp)",
  },
};

export const Grid: S = {
  render: () => (
    <div className="vg-grid vg-grid--4">
      <KpiTile label="Events · 24h"       value="1,284,503" delta="+8.4%"   tone="up"   spark={trend} />
      <KpiTile label="Active actors"      value="312"       delta="+12"     tone="up"   spark={[100,180,210,260,300,312]} />
      <KpiTile label="Anomalies open"     value="3"         delta="+2"      tone="down" spark={[0,0,1,2,1,3]} sparkColor="var(--color-anomaly)" />
      <KpiTile label="Anchor lag"         value="14s"       delta="within SLA" tone="warn" spark={[18,16,14,15,14,12,14]} sparkColor="var(--color-stamp)" />
    </div>
  ),
};
