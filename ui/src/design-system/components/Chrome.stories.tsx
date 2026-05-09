import type { Meta, StoryObj } from "@storybook/react";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";

const meta: Meta = {
  title: "Chrome",
  tags: ["autodocs"],
};
export default meta;

export const SidebarStory: StoryObj<typeof Sidebar> = {
  name: "Sidebar",
  render: () => (
    <div style={{ height: 600 }}>
      <Sidebar
        workspace="Acme Corp"
        product="Telemetry"
        groups={[
          {
            id: "audit",
            title: "Audit",
            defaultOpen: true,
            items: [
              { id: "events",    label: "Event log",    active: true },
              { id: "actors",    label: "Actors",       count: 312 },
              { id: "anchors",   label: "Anchor batches" },
            ],
          },
          {
            id: "detect",
            title: "Detect",
            defaultOpen: true,
            items: [
              { id: "anomalies",  label: "Anomalies",  count: 3 },
              { id: "policies",   label: "Policies" },
              { id: "alerts",     label: "Alerts" },
            ],
          },
          {
            id: "comply",
            title: "Comply",
            items: [
              { id: "frameworks", label: "Frameworks" },
              { id: "reports",    label: "Reports" },
              { id: "exports",    label: "Exports" },
            ],
          },
          {
            id: "admin",
            title: "Admin",
            items: [
              { id: "tenants",  label: "Tenants" },
              { id: "keys",     label: "API keys" },
              { id: "settings", label: "Settings" },
            ],
          },
        ]}
      />
    </div>
  ),
};

export const TopbarStory: StoryObj<typeof Topbar> = {
  name: "Topbar",
  render: () => (
    <Topbar
      crumbs={[
        { label: "Acme Corp", href: "#" },
        { label: "Audit",     href: "#" },
        { label: "Event log" },
      ]}
      env={{ label: "EU-WEST · PROD", region: "eu" }}
      tenant={{ name: "acme-prod", meta: "ws_8e21" }}
      user={{ initials: "AL" }}
    />
  ),
};
