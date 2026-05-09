import type { Meta, StoryObj } from "@storybook/react";
import { useState } from "react";
import { Table, type TableColumn, type SortDir } from "./Table";

interface AuditRow {
  ts: string;
  actor: string;
  event: string;
  hash: string;
  state: "anchored" | "pending" | "anomaly";
}

const rows: AuditRow[] = [
  { ts: "2026-05-08 14:32:11Z", actor: "alice@acme.co",    event: "messages.create",      hash: "9f3a…b211", state: "anchored" },
  { ts: "2026-05-08 14:31:52Z", actor: "bob@acme.co",      event: "messages.create",      hash: "7c2e…aa90", state: "anchored" },
  { ts: "2026-05-08 14:31:08Z", actor: "carla@acme.co",    event: "files.create",         hash: "4d1f…c012", state: "pending"  },
  { ts: "2026-05-08 14:30:44Z", actor: "service-acct-22",  event: "messages.create",      hash: "1b8a…ef27", state: "anomaly"  },
  { ts: "2026-05-08 14:30:11Z", actor: "alice@acme.co",    event: "workspaces.list",      hash: "5e07…7d4d", state: "anchored" },
];

const columns: TableColumn<AuditRow>[] = [
  { key: "ts",    header: "Time",   mono: true, sortable: true, width: 200, cell: r => r.ts },
  { key: "actor", header: "Actor",  sortable: true,             cell: r => r.actor },
  { key: "event", header: "Event",  mono: true,                 cell: r => r.event },
  { key: "hash",  header: "SHA-256", mono: true,                cell: r => r.hash },
  { key: "state", header: "State",  align: "right",
    cell: r => <span className={`vg-badge vg-badge--${r.state}`}><span className="vg-badge__dot" />{r.state}</span> },
];

const meta: Meta<typeof Table> = {
  title: "Data/Table",
  component: Table,
  tags: ["autodocs"],
};
export default meta;
type S = StoryObj<typeof Table>;

export const AuditTrail: S = {
  render: () => {
    const [sort, setSort] = useState<{ key: string; dir: SortDir }>({ key: "ts", dir: "desc" });
    return (
      <Table
        columns={columns}
        rows={rows}
        rowKey={r => r.hash}
        sort={sort}
        onSortChange={setSort}
      />
    );
  },
};

export const Empty: S = {
  render: () => (
    <Table
      columns={columns}
      rows={[]}
      rowKey={r => r.hash}
      empty="No audit events match the current filters."
    />
  ),
};
