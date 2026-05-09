import * as React from "react";
import { AppShell, PageHeader, TimeRange } from "../AppShell";
import { Button } from "../../design-system/components/Button";
import { Card } from "../../design-system/components/Card";
import { Table, type TableColumn } from "../../design-system/components/Table";
import { RedactionToggle } from "../../design-system/components/RedactionToggle";

type Tab = "behavioral" | "workforce";

interface Department {
  id: string; name: string;
  actors: number; activePct: number; eventsPerDay: number;
  spark: number[];
}

const departments: Department[] = [
  { id: "legal",     name: "Legal",     actors: 24,  activePct: 92, eventsPerDay: 1820, spark: [60,72,80,90,110,140,180] },
  { id: "eng",       name: "Engineering", actors: 142, activePct: 88, eventsPerDay: 9410, spark: [400,520,640,720,830,910,940] },
  { id: "research",  name: "Research",  actors: 38,  activePct: 76, eventsPerDay: 2240, spark: [120,140,180,200,220,224] },
  { id: "marketing", name: "Marketing", actors: 51,  activePct: 41, eventsPerDay:  890, spark: [80,82,90,88,84,89] },
  { id: "platform",  name: "Platform",  actors: 18,  activePct: 67, eventsPerDay: 1418, spark: [110,118,124,130,141,141] },
  { id: "support",   name: "Support",   actors: 39,  activePct: 84, eventsPerDay: 2118, spark: [140,160,180,200,212,212] },
];

interface UserRow {
  user: string; role: string; sessions: number; tokens: string; redacted: boolean; lastPrompt: string;
}
const userRows: UserRow[] = [
  { user: "alice@acme.co",  role: "Counsel",       sessions: 412, tokens: "118M", redacted: true,  lastPrompt: "Review NDA from vendor X — flag termination clauses" },
  { user: "bob@acme.co",    role: "Senior Eng",    sessions: 388, tokens: "104M", redacted: false, lastPrompt: "Refactor the auth middleware to use the new session lib" },
  { user: "carla@acme.co",  role: "Researcher",    sessions: 271, tokens: " 92M", redacted: true,  lastPrompt: "Summarize Q3 customer interviews and surface top 5 themes" },
  { user: "diane@acme.co",  role: "Brand Manager", sessions: 199, tokens: " 58M", redacted: false, lastPrompt: "Draft 3 launch headlines for the EU compliance pack" },
];

interface WorkflowRow { workflow: string; runs: number; automation: string; avgSavings: string; }
const workflowRows: WorkflowRow[] = [
  { workflow: "Contract first-pass review", runs: 1208, automation: "94%", avgSavings: "27 min" },
  { workflow: "PR review summary",          runs: 4422, automation: "71%", avgSavings: "12 min" },
  { workflow: "Customer call summary",      runs: 1841, automation: "88%", avgSavings: "18 min" },
  { workflow: "Brief → first draft",        runs:  712, automation: "62%", avgSavings: "44 min" },
];

const InsightPage: React.FC = () => {
  const [tab, setTab] = React.useState<Tab>("behavioral");
  const [range, setRange] = React.useState<"24h" | "7d" | "30d" | "90d">("30d");
  const [selectedDept, setSelectedDept] = React.useState<string | null>(null);

  return (
    <AppShell activeNav="insight" crumbs={[
      { label: "Acme Corp" },
      { label: "Insight" },
      { label: tab === "behavioral" ? "Behavioral" : "Workforce automation" },
    ]}>
      <PageHeader
        title="Insight"
        sub="Aggregated · redaction enforced"
        actions={
          <>
            <TimeRange value={range} onChange={setRange} />
            <Button variant="secondary" size="md">Export</Button>
          </>
        }
      />

      <Tabs tab={tab} onChange={setTab} />

      {tab === "behavioral" && (
        selectedDept === null ? (
          <DepartmentGrid departments={departments} onSelect={setSelectedDept} />
        ) : (
          <DepartmentDetail
            dept={departments.find(d => d.id === selectedDept)!}
            rows={userRows}
            onBack={() => setSelectedDept(null)}
          />
        )
      )}

      {tab === "workforce" && <WorkforcePane workflows={workflowRows} />}
    </AppShell>
  );
};

const Tabs: React.FC<{ tab: Tab; onChange: (t: Tab) => void }> = ({ tab, onChange }) => (
  <div style={{ display: "flex", gap: 4, borderBottom: "1px solid var(--color-line)", marginBottom: 24 }}>
    {([
      { id: "behavioral", label: "Behavioral analytics" },
      { id: "workforce",  label: "Workforce automation" },
    ] as { id: Tab; label: string }[]).map(t => (
      <button
        key={t.id}
        type="button"
        onClick={() => onChange(t.id)}
        style={{
          background: "transparent", border: "none", padding: "10px 16px",
          fontFamily: "var(--font-sans)", fontSize: "var(--fs-base)",
          color: tab === t.id ? "var(--color-ink)" : "var(--color-ink-3)",
          borderBottom: `2px solid ${tab === t.id ? "var(--color-stamp)" : "transparent"}`,
          marginBottom: -1, cursor: "pointer", fontWeight: tab === t.id ? 500 : 400,
        }}
      >
        {t.label}
      </button>
    ))}
  </div>
);

const DepartmentGrid: React.FC<{ departments: Department[]; onSelect: (id: string) => void }> = ({ departments, onSelect }) => (
  <div className="vg-grid vg-grid--3">
    {departments.map(d => (
      <button
        key={d.id} type="button" onClick={() => onSelect(d.id)}
        style={{ textAlign: "left", border: "1px solid var(--color-line)", borderRadius: "var(--r)", padding: 18, background: "var(--color-paper)", cursor: "pointer", display: "flex", flexDirection: "column", gap: 12 }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <span style={{ fontSize: "var(--fs-md)", fontWeight: 500, letterSpacing: "var(--ls-tight)" }}>{d.name}</span>
          <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-3)" }}>{d.actors} actors</span>
        </div>
        <div style={{ fontSize: "var(--fs-2xl)", fontWeight: 500, letterSpacing: "var(--ls-tighter)" }}>
          {d.activePct}<span style={{ fontSize: "var(--fs-md)", color: "var(--color-ink-3)" }}>%</span>
          <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-3)", marginLeft: 8, textTransform: "uppercase", letterSpacing: "var(--ls-wide)" }}>active</span>
        </div>
        <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-3)" }}>{d.eventsPerDay.toLocaleString()} events/day</div>
        <Sparkline values={d.spark} />
      </button>
    ))}
  </div>
);

const Sparkline: React.FC<{ values: number[] }> = ({ values }) => {
  const max = Math.max(...values), min = Math.min(...values), range = max - min || 1;
  const stepX = 100 / (values.length - 1);
  const pts = values.map((v, i) => `${i === 0 ? "M" : "L"}${(i * stepX).toFixed(2)} ${(28 - ((v - min) / range) * 24 - 2).toFixed(2)}`).join(" ");
  return (
    <svg viewBox="0 0 100 28" preserveAspectRatio="none" style={{ width: "100%", height: 28 }}>
      <path d={pts} stroke="var(--color-ink-3)" strokeWidth={1.2} fill="none" />
    </svg>
  );
};

const DepartmentDetail: React.FC<{ dept: Department; rows: UserRow[]; onBack: () => void }> = ({ dept, rows, onBack }) => {
  const cols: TableColumn<UserRow>[] = [
    { key: "user",     header: "User",     cell: r => r.user },
    { key: "role",     header: "Role",     cell: r => r.role },
    { key: "sessions", header: "Sessions", mono: true, align: "right", cell: r => r.sessions.toLocaleString() },
    { key: "tokens",   header: "Tokens",   mono: true, align: "right", cell: r => r.tokens },
    { key: "prompt",   header: "Last prompt", cell: r =>
      r.redacted
        ? <RedactionToggle value={r.lastPrompt} receipt={`${r.user.split("@")[0]}-row`} />
        : <span style={{ color: "var(--color-ink-2)" }}>{r.lastPrompt}</span>
    },
  ];
  return (
    <div className="vg-stack" style={{ gap: 16 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 16 }}>
        <Button variant="ghost" size="sm" onClick={onBack}>← Departments</Button>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", letterSpacing: "var(--ls-widest)", textTransform: "uppercase", color: "var(--color-ink-3)" }}>
          DEPT · {dept.name.toUpperCase()} · {dept.actors} actors · {dept.activePct}% active
        </span>
      </div>
      <Card title={`${dept.name} — actors`} sub="30d">
        <Table columns={cols} rows={rows} rowKey={r => r.user} />
      </Card>
    </div>
  );
};

const WorkforcePane: React.FC<{ workflows: WorkflowRow[] }> = ({ workflows }) => {
  const cols: TableColumn<WorkflowRow>[] = [
    { key: "workflow",   header: "Workflow",          cell: r => r.workflow },
    { key: "runs",       header: "Runs",       mono: true, align: "right", cell: r => r.runs.toLocaleString() },
    { key: "automation", header: "Automation", mono: true, align: "right", cell: r => <span style={{ color: "var(--color-anchored)" }}>{r.automation}</span> },
    { key: "savings",    header: "Avg savings", mono: true, align: "right", cell: r => r.avgSavings },
  ];
  return (
    <div className="vg-stack" style={{ gap: 24 }}>
      <Card title="% of tasks served by Claude" sub="30d · indexed">
        <svg viewBox="0 0 800 200" preserveAspectRatio="none" style={{ width: "100%", height: 200, display: "block" }}>
          {[0, 50, 100, 150, 200].map(y => <line key={y} x1={0} y1={y} x2={800} y2={y} stroke="var(--color-line)" />)}
          <path d="M0 170 L100 158 L200 142 L300 138 L400 110 L500 90 L600 78 L700 62 L800 50" fill="none" stroke="var(--color-indigo)" strokeWidth={1.6} />
        </svg>
      </Card>
      <Card title="Top automated workflows" sub="30d">
        <Table columns={cols} rows={workflows} rowKey={r => r.workflow} />
      </Card>
    </div>
  );
};

export default InsightPage;
