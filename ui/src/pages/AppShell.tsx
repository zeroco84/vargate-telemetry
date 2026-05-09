import * as React from "react";
import { Sidebar, type SidebarGroup } from "../design-system/components/Sidebar";
import { Topbar, type TopbarCrumb } from "../design-system/components/Topbar";

export interface AppShellProps {
  /** Active nav id — matches a SidebarItem id. */
  activeNav: string;
  /** Breadcrumb trail for the topbar. */
  crumbs: TopbarCrumb[];
  children: React.ReactNode;
}

const SIDEBAR_GROUPS: SidebarGroup[] = [
  {
    id: "monitor", title: "Monitor", defaultOpen: true,
    items: [
      { id: "usage",  label: "Usage" },
      { id: "alerts", label: "Alerts", count: 3 },
    ],
  },
  {
    id: "analyze", title: "Analyze", defaultOpen: true,
    items: [
      { id: "insight", label: "Insight" },
    ],
  },
  {
    id: "govern", title: "Govern", defaultOpen: true,
    items: [
      { id: "compliance", label: "Compliance" },
    ],
  },
  {
    id: "admin", title: "Admin",
    items: [
      { id: "settings", label: "Settings" },
    ],
  },
];

/**
 * Application shell used by every top-level page. Composes Sidebar + Topbar
 * and provides the page body's left-rail + primary-content grid.
 *
 * Pages render their content as `children`; this shell does not impose any
 * inner padding so individual pages can choose between full-bleed and padded
 * layouts.
 */
export const AppShell: React.FC<AppShellProps> = ({ activeNav, crumbs, children }) => {
  const groups: SidebarGroup[] = SIDEBAR_GROUPS.map(g => ({
    ...g,
    items: g.items.map(it => ({ ...it, active: it.id === activeNav })),
  }));

  return (
    <div style={{ display: "grid", gridTemplateColumns: "240px 1fr", minHeight: "100vh", background: "var(--color-paper-2)" }}>
      <Sidebar workspace="Acme Corp" product="Telemetry" groups={groups} />
      <div style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
        <Topbar
          crumbs={crumbs}
          env={{ label: "EU-WEST · PROD", region: "eu" }}
          tenant={{ name: "acme-prod", meta: "ws_8e21" }}
          user={{ initials: "AL" }}
        />
        <main style={{ flex: 1, padding: "24px 28px 48px", minWidth: 0 }}>{children}</main>
      </div>
    </div>
  );
};

/** Standard page header — title + sub + right-side actions. */
export interface PageHeaderProps {
  title: React.ReactNode;
  sub?: React.ReactNode;
  actions?: React.ReactNode;
}

export const PageHeader: React.FC<PageHeaderProps> = ({ title, sub, actions }) => (
  <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 24, marginBottom: 24 }}>
    <div>
      <h1 style={{ fontSize: "var(--fs-2xl)", fontWeight: 500, letterSpacing: "var(--ls-tighter)", margin: 0, color: "var(--color-ink)" }}>
        {title}
      </h1>
      {sub && (
        <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", letterSpacing: "var(--ls-wide)", color: "var(--color-ink-3)", textTransform: "uppercase", marginTop: 6 }}>
          {sub}
        </div>
      )}
    </div>
    {actions && <div style={{ display: "flex", gap: 8, alignItems: "center" }}>{actions}</div>}
  </div>
);

/** Reusable time-range pill segmented control. */
export interface TimeRangeProps {
  value: "24h" | "7d" | "30d" | "90d";
  onChange?: (v: TimeRangeProps["value"]) => void;
}
export const TimeRange: React.FC<TimeRangeProps> = ({ value, onChange }) => {
  const opts: TimeRangeProps["value"][] = ["24h", "7d", "30d", "90d"];
  return (
    <div style={{ display: "inline-flex", border: "1px solid var(--color-line-2)", borderRadius: "var(--r)", overflow: "hidden", background: "var(--color-paper)" }}>
      {opts.map(o => (
        <button
          key={o}
          type="button"
          onClick={() => onChange?.(o)}
          style={{
            border: "none",
            padding: "6px 12px",
            fontFamily: "var(--font-mono)",
            fontSize: "var(--fs-xs)",
            letterSpacing: "var(--ls-wide)",
            textTransform: "uppercase",
            background: o === value ? "var(--color-paper-3)" : "transparent",
            color: o === value ? "var(--color-ink)" : "var(--color-ink-3)",
            cursor: "pointer",
            borderRight: "1px solid var(--color-line)",
          }}
        >
          {o}
        </button>
      ))}
    </div>
  );
};
