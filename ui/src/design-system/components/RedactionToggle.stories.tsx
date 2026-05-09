import type { Meta, StoryObj } from "@storybook/react";
import { RedactionToggle } from "./RedactionToggle";

const meta: Meta<typeof RedactionToggle> = {
  title: "Distinctive/RedactionToggle",
  component: RedactionToggle,
  tags: ["autodocs"],
  parameters: {
    docs: {
      description: {
        component:
          "Three-step reveal for sensitive fields (prompt body, file contents, customer PII). " +
          "Revealing is itself an audited event in Vargate, so the UX surfaces the cost of looking. " +
          "Compose into table cells or drill-through panels — never use as a casual show-password toggle.",
      },
    },
  },
};
export default meta;
type S = StoryObj<typeof RedactionToggle>;

export const Locked: S = {
  args: {
    value: "Generate a draft email about the layoff plans for Q3 — keep it sympathetic but firm",
    receipt: "alice@acme.co · 2026-05-08T14:32Z",
  },
};

export const PreRevealed: S = {
  args: {
    initial: "revealed",
    value: "Generate a draft email about the layoff plans for Q3 — keep it sympathetic but firm",
    receipt: "alice@acme.co · 2026-05-08T14:32Z",
  },
};

export const InTableCell: S = {
  render: () => (
    <table className="vg-table" style={{ maxWidth: 720 }}>
      <thead>
        <tr><th>Time</th><th>Actor</th><th>Prompt body</th></tr>
      </thead>
      <tbody>
        <tr>
          <td className="vg-mono">14:32:11Z</td>
          <td>alice@acme.co</td>
          <td>
            <RedactionToggle
              value="Generate a draft email about the layoff plans for Q3"
              receipt="row 9f3ab211"
            />
          </td>
        </tr>
        <tr>
          <td className="vg-mono">14:31:52Z</td>
          <td>bob@acme.co</td>
          <td>
            <RedactionToggle
              value="Summarize the attached customer contract and flag termination clauses"
              receipt="row 7c2eaa90"
            />
          </td>
        </tr>
      </tbody>
    </table>
  ),
};
