import type { Meta, StoryObj } from "@storybook/react";
import { AlertRow } from "./AlertRow";

const meta: Meta<typeof AlertRow> = {
  title: "Data/AlertRow",
  component: AlertRow,
  tags: ["autodocs"],
};
export default meta;
type S = StoryObj<typeof AlertRow>;

export const Anomaly: S = {
  args: {
    severity: "danger",
    title: "Service account exfiltrated 2.3 MB of file content",
    body: "service-acct-22 issued 412 files.create calls in 4m, against a baseline of 1.4/hour. Source IP outside known egress range. Drill in to inspect the request bodies and decide whether to revoke.",
    timestamp: "14:32 UTC",
    source: "ANOMALY · POLICY-09",
    defaultOpen: true,
  },
};

export const Compliance: S = {
  args: {
    severity: "warning",
    title: "EU AI Act Art. 12 — log retention threshold approaching",
    body: "Retention window currently set to 6 months; Art. 12 requires ≥ 6 months for high-risk systems. Extend to 12 months recommended.",
    timestamp: "09:11 UTC",
    source: "COMPLIANCE · EU-AI-12",
  },
};

export const Info: S = {
  args: {
    severity: "info",
    title: "Daily anchor batch confirmed on-chain",
    body: "Block 19,482,011 · 1,284,503 events · root c93a…7e21",
    timestamp: "00:14 UTC",
    source: "ANCHOR",
  },
};

export const Stack: S = {
  render: () => (
    <div className="vg-stack">
      <AlertRow severity="danger"  title="Service account exfiltrated 2.3 MB of file content"
        body="service-acct-22 issued 412 files.create calls in 4m." timestamp="14:32 UTC" source="ANOMALY · POLICY-09" />
      <AlertRow severity="warning" title="EU AI Act Art. 12 — retention threshold approaching"
        body="Retention currently 6 months; recommend 12." timestamp="09:11 UTC" source="COMPLIANCE · EU-AI-12" />
      <AlertRow severity="info"    title="Daily anchor batch confirmed on-chain"
        body="Block 19,482,011 · 1,284,503 events" timestamp="00:14 UTC" source="ANCHOR" />
    </div>
  ),
};
