import * as React from "react";
import { AppShell, PageHeader } from "../AppShell";
import { Button } from "../../design-system/components/Button";
import { Card } from "../../design-system/components/Card";
import { Table, type TableColumn } from "../../design-system/components/Table";
import { IconSearch } from "../../design-system/components/icons";

type PolicyStatus = "live" | "shadow" | "paused";
interface Policy {
  id: string;
  name: string;
  framework: string;
  status: PolicyStatus;
  lastEdited: string;
  lastTriggered: string;
  allowlists: number;
}

const policies: Policy[] = [
  { id: "policy_09", name: "PII in prompt body",            framework: "EU AI Act · GDPR",  status: "live",   lastEdited: "2d ago",  lastTriggered: "14:32 UTC",  allowlists: 4 },
  { id: "policy_22", name: "Customer contract redaction",   framework: "SOX",                status: "live",   lastEdited: "5d ago",  lastTriggered: "13:08 UTC",  allowlists: 2 },
  { id: "policy_eu_12", name: "Retention threshold",        framework: "EU AI Act · Art. 12",status: "live",   lastEdited: "11d ago", lastTriggered: "09:11 UTC",  allowlists: 0 },
  { id: "policy_44", name: "Off-hours access (HR)",         framework: "Internal",           status: "shadow", lastEdited: "1d ago",  lastTriggered: "—",          allowlists: 1 },
  { id: "policy_sla_01", name: "Anchor SLA breach",         framework: "Internal",           status: "live",   lastEdited: "30d ago", lastTriggered: "00:14 UTC",  allowlists: 0 },
  { id: "policy_31", name: "Source code in prompt",         framework: "ISO 42001",          status: "paused", lastEdited: "18d ago", lastTriggered: "—",          allowlists: 3 },
];

const FRAMEWORKS = ["All", "EU AI Act", "SOX", "HIPAA", "ISO 42001", "GDPR", "Internal"];

const StatusPill: React.FC<{ status: PolicyStatus }> = ({ status }) => {
  const cls =
    status === "live"   ? "vg-badge--anchored" :
    status === "shadow" ? "vg-badge--info" :
                          "vg-badge--pending";
  return (
    <span className={`vg-badge ${cls}`}>
      <span className="vg-badge__dot" />
      {status}
    </span>
  );
};

const cols: TableColumn<Policy>[] = [
  { key: "name",      header: "Name", cell: r =>
    <div>
      <div style={{ color: "var(--color-ink)", fontWeight: 500 }}>{r.name}</div>
      <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-3)" }}>{r.id}</div>
    </div>
  },
  { key: "framework", header: "Framework", mono: true, cell: r => r.framework },
  { key: "status",    header: "Status",    cell: r => <StatusPill status={r.status} /> },
  { key: "edited",    header: "Last edited",     mono: true, sortable: true, cell: r => r.lastEdited },
  { key: "triggered", header: "Last triggered",  mono: true, sortable: true, cell: r => r.lastTriggered },
  { key: "allow",     header: "Allowlists", mono: true, align: "right", cell: r => r.allowlists },
];

const CompliancePage: React.FC = () => {
  const [framework, setFramework] = React.useState("All");
  const visible = framework === "All" ? policies : policies.filter(p => p.framework.includes(framework));

  return (
    <AppShell activeNav="compliance" crumbs={[{ label: "Acme Corp" }, { label: "Compliance" }]}>
      <PageHeader
        title="Compliance"
        sub={`${policies.filter(p => p.status === "live").length} live · ${policies.filter(p => p.status === "shadow").length} shadow · ${policies.filter(p => p.status === "paused").length} paused`}
        actions={
          <>
            <Button variant="secondary" size="md">Import policy pack</Button>
            <Button variant="primary"   size="md">Create policy</Button>
          </>
        }
      />

      <div style={{ display: "flex", gap: 12, marginBottom: 16, alignItems: "center", flexWrap: "wrap" }}>
        <div className="vg-field" style={{ minWidth: 280 }}>
          <div className="vg-field__control">
            <span style={{ paddingLeft: 10, color: "var(--color-ink-3)", display: "inline-flex" }}><IconSearch /></span>
            <input className="vg-input" placeholder="Search policies…" />
          </div>
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {FRAMEWORKS.map(f => (
            <button
              key={f}
              type="button"
              onClick={() => setFramework(f)}
              style={{
                background: framework === f ? "var(--color-ink)" : "var(--color-paper)",
                color: framework === f ? "var(--color-paper)" : "var(--color-ink-2)",
                border: `1px solid ${framework === f ? "var(--color-ink)" : "var(--color-line-2)"}`,
                borderRadius: "var(--r-full)",
                padding: "5px 12px",
                fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)",
                letterSpacing: "var(--ls-wide)", textTransform: "uppercase",
                cursor: "pointer",
              }}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      <Card title="Policies" sub={`${visible.length} of ${policies.length}`} style={{ marginBottom: 24 } as React.CSSProperties}>
        <Table columns={cols} rows={visible} rowKey={r => r.id} onRowClick={() => {}} />
      </Card>

      <div className="vg-grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <Card title="Allowlists" sub="6 active"
          actions={<Button variant="ghost" size="sm">Manage</Button> as any}
        >
          <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 8 }}>
            {[
              { name: "Approved domains",     count: 38 },
              { name: "Approved file types",  count: 12 },
              { name: "Service accounts",     count:  6 },
              { name: "PII redaction tokens", count: 24 },
            ].map(a => (
              <li key={a.name} style={{ display: "flex", justifyContent: "space-between", padding: "8px 0", borderBottom: "1px dashed var(--color-line)" }}>
                <span>{a.name}</span>
                <span style={{ fontFamily: "var(--font-mono)", color: "var(--color-ink-3)", fontSize: "var(--fs-xs)" }}>{a.count}</span>
              </li>
            ))}
          </ul>
        </Card>
        <Card title="Evidentiary exports" sub="Last 30 days"
          actions={<Button variant="stamp" size="sm">New export</Button> as any}
        >
          <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 10 }}>
            {[
              { name: "Q2 2026 EU AI Act bundle",     when: "12 Apr · alice@",  size: "412 MB" },
              { name: "Internal — Audit Cmte",        when: "08 Mar · diane@",  size: "1.1 GB" },
              { name: "External auditor — KPMG",      when: "02 Mar · alice@",  size: "880 MB" },
            ].map(e => (
              <li key={e.name} style={{ display: "flex", justifyContent: "space-between", padding: "10px 0", borderBottom: "1px dashed var(--color-line)" }}>
                <div>
                  <div style={{ color: "var(--color-ink)" }}>{e.name}</div>
                  <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-3)" }}>{e.when}</div>
                </div>
                <span style={{ fontFamily: "var(--font-mono)", color: "var(--color-ink-3)", fontSize: "var(--fs-xs)" }}>{e.size}</span>
              </li>
            ))}
          </ul>
        </Card>
      </div>
    </AppShell>
  );
};

export default CompliancePage;
