import type { Meta, StoryObj } from "@storybook/react";
import { useState } from "react";
import { DrillThrough } from "./DrillThrough";
import { Button } from "./Button";
import { RedactionToggle } from "./RedactionToggle";

const meta: Meta<typeof DrillThrough> = {
  title: "Distinctive/DrillThrough",
  component: DrillThrough,
  tags: ["autodocs"],
};
export default meta;
type S = StoryObj<typeof DrillThrough>;

export const RowDetail: S = {
  render: () => {
    const [open, setOpen] = useState(true);
    return (
      <div style={{ minHeight: 480 }}>
        <Button onClick={() => setOpen(true)}>Open detail</Button>
        <DrillThrough
          open={open}
          onClose={() => setOpen(false)}
          crumbs={[
            { label: "AUDIT" },
            { label: "EVENTS" },
            { label: "9F3A…B211" },
          ]}
          footer={
            <div className="vg-row" style={{ justifyContent: "flex-end" }}>
              <Button variant="ghost">Export receipt</Button>
              <Button variant="stamp">Acknowledge</Button>
            </div>
          }
        >
          <div className="vg-stack">
            <div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-3)", letterSpacing: "var(--ls-wide)", textTransform: "uppercase", marginBottom: 6 }}>
                Event
              </div>
              <div style={{ fontSize: "var(--fs-lg)", fontWeight: 500, letterSpacing: "var(--ls-tight)" }}>
                messages.create
              </div>
            </div>
            <dl style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "10px 24px", margin: 0, fontFamily: "var(--font-mono)", fontSize: "var(--fs-sm)" }}>
              <dt style={{ color: "var(--color-ink-3)" }}>Actor</dt>     <dd style={{ margin: 0 }}>alice@acme.co</dd>
              <dt style={{ color: "var(--color-ink-3)" }}>Workspace</dt> <dd style={{ margin: 0 }}>acme-prod</dd>
              <dt style={{ color: "var(--color-ink-3)" }}>Source IP</dt> <dd style={{ margin: 0 }}>10.4.22.18</dd>
              <dt style={{ color: "var(--color-ink-3)" }}>Hash</dt>      <dd style={{ margin: 0 }}>9f3a4d12…b21188e1</dd>
              <dt style={{ color: "var(--color-ink-3)" }}>Anchor</dt>    <dd style={{ margin: 0 }}><span className="vg-badge vg-badge--anchored"><span className="vg-badge__dot" />block 19,482,011</span></dd>
            </dl>
            <div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-3)", letterSpacing: "var(--ls-wide)", textTransform: "uppercase", marginBottom: 6 }}>
                Prompt body
              </div>
              <RedactionToggle
                value="Generate a draft email about the layoff plans for Q3"
                receipt="9f3a…b211"
              />
            </div>
          </div>
        </DrillThrough>
      </div>
    );
  },
};
