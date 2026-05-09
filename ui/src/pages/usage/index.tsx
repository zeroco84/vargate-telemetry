import * as React from "react";
import { AppShell, PageHeader, TimeRange } from "../AppShell";
import { Button } from "../../design-system/components/Button";
import { KpiTile } from "../../design-system/components/KpiTile";
import { Card } from "../../design-system/components/Card";
import { Table, type TableColumn } from "../../design-system/components/Table";

interface ModelRow { model: string; events: number; tokens: string; spend: string; }
interface ActorRow { actor: string; team: string; sessions: number; tokens: string; spend: string; }

const modelRows: ModelRow[] = [
  { model: "claude-sonnet-4-5",  events: 842_311, tokens: "1.42B", spend: "$24,108.40" },
  { model: "claude-opus-4-5",    events:  91_204, tokens: "412M",  spend: "$18,920.10" },
  { model: "claude-haiku-4-5",   events: 351_988, tokens: "688M",  spend: "$ 3,217.80" },
];

const actorRows: ActorRow[] = [
  { actor: "alice@acme.co",   team: "Legal",     sessions: 412, tokens: "118M", spend: "$2,840.10" },
  { actor: "bob@acme.co",     team: "Eng",       sessions: 388, tokens: "104M", spend: "$2,510.32" },
  { actor: "carla@acme.co",   team: "Research",  sessions: 271, tokens: " 92M", spend: "$2,180.04" },
  { actor: "service-acct-22", team: "Platform",  sessions:  92, tokens: " 71M", spend: "$1,420.66" },
  { actor: "diane@acme.co",   team: "Marketing", sessions: 199, tokens: " 58M", spend: "$1,118.40" },
];

const modelCols: TableColumn<ModelRow>[] = [
  { key: "model",  header: "Model",  mono: true,                 cell: r => r.model },
  { key: "events", header: "Events", mono: true, align: "right", cell: r => r.events.toLocaleString() },
  { key: "tokens", header: "Tokens", mono: true, align: "right", cell: r => r.tokens },
  { key: "spend",  header: "Spend",  mono: true, align: "right", cell: r => r.spend },
];

const actorCols: TableColumn<ActorRow>[] = [
  { key: "actor",    header: "Actor",    sortable: true,  cell: r => r.actor },
  { key: "team",     header: "Team",     cell: r => r.team },
  { key: "sessions", header: "Sessions", mono: true, align: "right", sortable: true, cell: r => r.sessions.toLocaleString() },
  { key: "tokens",   header: "Tokens",   mono: true, align: "right", cell: r => r.tokens },
  { key: "spend",    header: "Spend",    mono: true, align: "right", sortable: true, cell: r => r.spend },
];

const trend24h = [42, 44, 39, 51, 48, 55, 60, 58, 62, 70, 68, 74];

const UsagePage: React.FC = () => {
  const [range, setRange] = React.useState<"24h" | "7d" | "30d" | "90d">("7d");
  return (
    <AppShell activeNav="usage" crumbs={[{ label: "Acme Corp" }, { label: "Usage" }]}>
      <PageHeader
        title="Usage"
        sub={`Window · ${range.toUpperCase()} · UTC`}
        actions={
          <>
            <TimeRange value={range} onChange={setRange} />
            <Button variant="secondary" size="md">Export</Button>
          </>
        }
      />

      <div className="vg-grid vg-grid--4" style={{ marginBottom: 24 }}>
        <KpiTile label="Events ingested" value="1,284,503" delta="+8.4% vs prior" tone="up"   spark={trend24h} />
        <KpiTile label="Active actors"   value="312"       delta="+12 wk-over-wk" tone="up"   spark={[100,180,210,260,300,312]} />
        <KpiTile label="Token spend"     value="$46,246"   delta="+11.2%"         tone="warn" spark={[28,32,30,34,38,42,46]} sparkColor="var(--color-stamp)" />
        <KpiTile label="Anchor lag"      value="14s"       delta="within SLA"     tone="up"   spark={[18,16,14,15,14,12,14]} />
      </div>

      <Card
        title="Spend over time"
        sub="USD · 7d"
        style={{ marginBottom: 24 } as React.CSSProperties}
      >
        <ChartPlaceholder />
      </Card>

      <div className="vg-grid" style={{ gridTemplateColumns: "1fr 1.4fr" }}>
        <Card title="By model" sub="7d">
          <Table columns={modelCols} rows={modelRows} rowKey={r => r.model} />
        </Card>
        <Card title="Top actors" sub="By spend · 7d">
          <Table columns={actorCols} rows={actorRows} rowKey={r => r.actor} onRowClick={() => {}} />
        </Card>
      </div>
    </AppShell>
  );
};

const ChartPlaceholder: React.FC = () => (
  <svg viewBox="0 0 800 200" preserveAspectRatio="none" style={{ width: "100%", height: 200, display: "block" }}>
    {[0, 50, 100, 150, 200].map(y => (
      <line key={y} x1={0} y1={y} x2={800} y2={y} stroke="var(--color-line)" />
    ))}
    <path
      d="M0 150 L67 142 L133 138 L200 128 L267 132 L333 110 L400 96 L467 90 L533 82 L600 70 L667 66 L733 50 L800 42"
      fill="none" stroke="var(--color-indigo)" strokeWidth={1.5}
    />
    <path
      d="M0 150 L67 142 L133 138 L200 128 L267 132 L333 110 L400 96 L467 90 L533 82 L600 70 L667 66 L733 50 L800 42 L800 200 L0 200 Z"
      fill="var(--color-indigo)" fillOpacity={0.06}
    />
  </svg>
);

export default UsagePage;
