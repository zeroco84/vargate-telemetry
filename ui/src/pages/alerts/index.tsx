import * as React from "react";
import { AppShell, PageHeader, TimeRange } from "../AppShell";
import { Button } from "../../design-system/components/Button";
import { AlertRow } from "../../design-system/components/AlertRow";
import { DrillThrough } from "../../design-system/components/DrillThrough";
import { RedactionToggle } from "../../design-system/components/RedactionToggle";
import { IconChain, IconSearch } from "../../design-system/components/icons";

type Severity = "danger" | "warning" | "info";
interface Alert {
  id: string;
  severity: Severity;
  title: string;
  body: string;
  ts: string;
  source: string;
  policyId: string;
  actor: string;
  hash: string;
  block: string;
}

const alerts: Alert[] = [
  { id: "a1", severity: "danger",  title: "Service account exfiltrated 2.3 MB of file content",
    body: "service-acct-22 issued 412 files.create calls in 4m, against a baseline of 1.4/hour. Source IP outside known egress range.",
    ts: "14:32 UTC", source: "ANOMALY · POLICY-09", policyId: "policy_09", actor: "service-acct-22", hash: "0x9f3a4d12…b21188e1", block: "19,482,011" },
  { id: "a2", severity: "danger",  title: "Prompt body flagged: customer PII without redaction tag",
    body: "Prompt contained 14 likely-PII tokens (national ID format) and was sent without policy redaction tag. Submitted by carla@acme.co.",
    ts: "13:08 UTC", source: "ANOMALY · POLICY-22", policyId: "policy_22", actor: "carla@acme.co", hash: "0x7c2e0a90…aa90f111", block: "19,481,402" },
  { id: "a3", severity: "warning", title: "EU AI Act Art. 12 — log retention threshold approaching",
    body: "Retention currently set to 6 months; Art. 12 requires ≥ 6 months for high-risk systems. Extend to 12 months recommended.",
    ts: "09:11 UTC", source: "COMPLIANCE · EU-AI-12", policyId: "policy_eu_12", actor: "system", hash: "—", block: "—" },
  { id: "a4", severity: "warning", title: "Anchor batch published 18s late",
    body: "Daily anchor batch was published at 00:14:18 UTC, 18s outside the 14s SLA. No data loss; investigation logged.",
    ts: "00:14 UTC", source: "COMPLIANCE · ANCHOR-SLA", policyId: "policy_sla_01", actor: "system", hash: "0xc93a7e21…", block: "19,482,011" },
  { id: "a5", severity: "info",    title: "Daily anchor batch confirmed on-chain",
    body: "Block 19,482,011 · 1,284,503 events · root c93a…7e21",
    ts: "00:14 UTC", source: "ANCHOR", policyId: "—", actor: "system", hash: "0xc93a7e21…", block: "19,482,011" },
];

const FILTERS = {
  severity: [{ id: "danger", label: "Danger", count: 2 }, { id: "warning", label: "Warning", count: 2 }, { id: "info", label: "Info", count: 1 }],
  status:   [{ id: "open", label: "Open", count: 4 }, { id: "ack", label: "Acknowledged", count: 1 }, { id: "resolved", label: "Resolved", count: 0 }],
  source:   [{ id: "anomaly", label: "Anomaly", count: 2 }, { id: "compliance", label: "Compliance", count: 2 }, { id: "anchor", label: "Anchor", count: 1 }],
};

const AlertsPage: React.FC = () => {
  const [range, setRange] = React.useState<"24h" | "7d" | "30d" | "90d">("24h");
  const [active, setActive] = React.useState<string | null>("a1");
  const [activeFilters, setActiveFilters] = React.useState<Record<string, Set<string>>>({
    severity: new Set(["danger", "warning", "info"]),
    status: new Set(["open"]),
    source: new Set(["anomaly", "compliance", "anchor"]),
  });

  const toggleFilter = (g: string, id: string) => {
    setActiveFilters(prev => {
      const next = new Set(prev[g]);
      if (next.has(id)) next.delete(id); else next.add(id);
      return { ...prev, [g]: next };
    });
  };

  const visible = alerts.filter(a => activeFilters.severity.has(a.severity));
  const grouped: Record<Severity, Alert[]> = { danger: [], warning: [], info: [] };
  visible.forEach(a => grouped[a.severity].push(a));

  const activeAlert = alerts.find(a => a.id === active) ?? null;

  return (
    <AppShell activeNav="alerts" crumbs={[{ label: "Acme Corp" }, { label: "Alerts" }]}>
      <PageHeader
        title="Alerts"
        sub="Inbox · 5 open · acknowledged today: 2"
        actions={
          <>
            <TimeRange value={range} onChange={setRange} />
            <Button variant="secondary" size="md">Acknowledge selected</Button>
            <Button variant="stamp"     size="md" leadingIcon={<IconChain />}>Export evidence pack</Button>
          </>
        }
      />

      <div style={{ display: "grid", gridTemplateColumns: "240px 1fr", gap: 24, alignItems: "flex-start" }}>
        <FilterRail filters={FILTERS} active={activeFilters} onToggle={toggleFilter} />

        <div className="vg-stack" style={{ gap: 24 }}>
          {(["danger", "warning", "info"] as Severity[]).map(sev => grouped[sev].length > 0 && (
            <section key={sev}>
              <SeverityHeading severity={sev} count={grouped[sev].length} />
              <div className="vg-stack" style={{ gap: 8 }}>
                {grouped[sev].map(a => (
                  <div key={a.id} onClick={() => setActive(a.id)} style={{ cursor: "pointer" }}>
                    <AlertRow
                      severity={a.severity}
                      title={a.title}
                      body={a.body}
                      timestamp={a.ts}
                      source={a.source}
                      defaultOpen={a.id === active}
                    />
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>

      <DrillThrough
        open={!!activeAlert}
        onClose={() => setActive(null)}
        crumbs={activeAlert ? [
          { label: "ALERTS" },
          { label: activeAlert.severity.toUpperCase() },
          { label: activeAlert.policyId.toUpperCase() },
        ] : []}
        footer={
          <div className="vg-row" style={{ justifyContent: "space-between" }}>
            <Button variant="ghost">Mark resolved</Button>
            <div className="vg-row" style={{ gap: 8 }}>
              <Button variant="secondary">Acknowledge</Button>
              <Button variant="stamp" leadingIcon={<IconChain />}>Export chain proof</Button>
            </div>
          </div>
        }
      >
        {activeAlert && <AlertDetail alert={activeAlert} />}
      </DrillThrough>
    </AppShell>
  );
};

const SeverityHeading: React.FC<{ severity: Severity; count: number }> = ({ severity, count }) => {
  const label = severity === "danger" ? "Danger" : severity === "warning" ? "Warning" : "Info";
  const color = severity === "danger" ? "var(--color-anomaly)" : severity === "warning" ? "var(--color-stamp-2)" : "var(--color-indigo-2)";
  return (
    <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 10 }}>
      <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", letterSpacing: "var(--ls-widest)", textTransform: "uppercase", color }}>{label}</span>
      <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-3)" }}>· {count} open</span>
      <div style={{ flex: 1, height: 1, background: "var(--color-line)" }} />
    </div>
  );
};

const FilterRail: React.FC<{
  filters: typeof FILTERS;
  active: Record<string, Set<string>>;
  onToggle: (g: string, id: string) => void;
}> = ({ filters, active, onToggle }) => (
  <aside className="vg-stack" style={{ gap: 20, position: "sticky", top: 16 }}>
    <div className="vg-field">
      <div className="vg-field__control">
        <span style={{ paddingLeft: 10, color: "var(--color-ink-3)", display: "inline-flex" }}><IconSearch /></span>
        <input className="vg-input" placeholder="Filter by policy id…" />
      </div>
    </div>
    {(Object.keys(filters) as (keyof typeof FILTERS)[]).map(group => (
      <FilterGroup key={group} title={group} items={filters[group]} active={active[group]} onToggle={id => onToggle(group, id)} />
    ))}
  </aside>
);

const FilterGroup: React.FC<{
  title: string;
  items: { id: string; label: string; count: number }[];
  active: Set<string>;
  onToggle: (id: string) => void;
}> = ({ title, items, active, onToggle }) => (
  <div>
    <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", letterSpacing: "var(--ls-widest)", textTransform: "uppercase", color: "var(--color-ink-3)", marginBottom: 8 }}>{title}</div>
    <div className="vg-stack" style={{ gap: 4 }}>
      {items.map(it => (
        <label key={it.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 10px", borderRadius: "var(--r-sm)", cursor: "pointer", background: active.has(it.id) ? "var(--color-paper)" : "transparent", border: `1px solid ${active.has(it.id) ? "var(--color-line)" : "transparent"}` }}>
          <input type="checkbox" checked={active.has(it.id)} onChange={() => onToggle(it.id)} style={{ accentColor: "var(--color-indigo)" }} />
          <span style={{ fontSize: "var(--fs-sm)", color: "var(--color-ink)" }}>{it.label}</span>
          <span style={{ marginLeft: "auto", fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-3)" }}>{it.count}</span>
        </label>
      ))}
    </div>
  </div>
);

const AlertDetail: React.FC<{ alert: Alert }> = ({ alert }) => (
  <div className="vg-stack" style={{ gap: 20 }}>
    <div>
      <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-3)", letterSpacing: "var(--ls-wide)", textTransform: "uppercase", marginBottom: 6 }}>{alert.source}</div>
      <div style={{ fontSize: "var(--fs-lg)", fontWeight: 500, letterSpacing: "var(--ls-tight)", lineHeight: 1.3 }}>{alert.title}</div>
      <p style={{ color: "var(--color-ink-2)", marginTop: 8, lineHeight: 1.55 }}>{alert.body}</p>
    </div>
    <dl style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "10px 24px", margin: 0, fontFamily: "var(--font-mono)", fontSize: "var(--fs-sm)" }}>
      <dt style={{ color: "var(--color-ink-3)" }}>When</dt>      <dd style={{ margin: 0 }}>{alert.ts}</dd>
      <dt style={{ color: "var(--color-ink-3)" }}>Actor</dt>     <dd style={{ margin: 0 }}>{alert.actor}</dd>
      <dt style={{ color: "var(--color-ink-3)" }}>Policy</dt>    <dd style={{ margin: 0 }}>{alert.policyId}</dd>
      <dt style={{ color: "var(--color-ink-3)" }}>Hash</dt>      <dd style={{ margin: 0 }}>{alert.hash}</dd>
      <dt style={{ color: "var(--color-ink-3)" }}>Block</dt>     <dd style={{ margin: 0 }}>{alert.block}</dd>
    </dl>
    <div>
      <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-3)", letterSpacing: "var(--ls-wide)", textTransform: "uppercase", marginBottom: 8 }}>Underlying prompt body</div>
      <RedactionToggle value="Generate a draft email about the layoff plans for Q3" receipt={alert.hash} />
    </div>
  </div>
);

export default AlertsPage;
