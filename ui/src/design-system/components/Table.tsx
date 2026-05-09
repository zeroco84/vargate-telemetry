import * as React from "react";

export type SortDir = "asc" | "desc" | null;

export interface TableColumn<Row> {
  key: string;
  header: React.ReactNode;
  /** When true, header click cycles asc → desc → null. */
  sortable?: boolean;
  /** Render the cell. Receives the row + index. */
  cell: (row: Row, idx: number) => React.ReactNode;
  /** Optional inline width (px or %). */
  width?: number | string;
  /** Render this column with the mono font. */
  mono?: boolean;
  /** CSS textAlign for both header and cell. */
  align?: "left" | "right" | "center";
}

export interface TableProps<Row> {
  columns: TableColumn<Row>[];
  rows: Row[];
  /** Stable key getter — required for React reconciliation. */
  rowKey: (row: Row, idx: number) => string;
  /** Currently sorted column key + direction (controlled). */
  sort?: { key: string; dir: SortDir };
  onSortChange?: (sort: { key: string; dir: SortDir }) => void;
  /** Click handler for an entire row — used to open drill-through. */
  onRowClick?: (row: Row, idx: number) => void;
  /** Render this when rows is empty. */
  empty?: React.ReactNode;
  /** Cap visible height; sticky header stays pinned while scrolling. */
  maxHeight?: number | string;
  className?: string;
}

const nextDir: Record<string, SortDir> = { null: "asc", asc: "desc", desc: null };

/**
 * Presentational data table with sortable columns, sticky header, and row
 * hover. Sort state is controlled by the caller — this component fires
 * `onSortChange`; it does not reorder rows itself.
 */
export function Table<Row>({
  columns,
  rows,
  rowKey,
  sort,
  onSortChange,
  onRowClick,
  empty,
  maxHeight,
  className,
}: TableProps<Row>) {
  const handleSort = (col: TableColumn<Row>) => {
    if (!col.sortable || !onSortChange) return;
    const current = sort?.key === col.key ? sort.dir : null;
    onSortChange({ key: col.key, dir: nextDir[String(current)] });
  };

  return (
    <div
      className={["vg-table-wrap", className].filter(Boolean).join(" ")}
      style={{ maxHeight }}
    >
      <table className="vg-table">
        <thead>
          <tr>
            {columns.map(col => {
              const isSorted = sort?.key === col.key && sort.dir != null;
              return (
                <th
                  key={col.key}
                  data-sortable={col.sortable || undefined}
                  data-sort={isSorted ? sort?.dir : undefined}
                  onClick={() => handleSort(col)}
                  style={{ width: col.width, textAlign: col.align ?? "left" }}
                >
                  {col.header}
                  {col.sortable && (
                    <span className="vg-sort-glyph" aria-hidden>
                      {sort?.key === col.key && sort.dir === "asc" ? "▲" :
                       sort?.key === col.key && sort.dir === "desc" ? "▼" : "▲▼"}
                    </span>
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="vg-table__empty">
                {empty ?? "No records."}
              </td>
            </tr>
          ) : rows.map((row, idx) => (
            <tr
              key={rowKey(row, idx)}
              onClick={onRowClick ? () => onRowClick(row, idx) : undefined}
              style={{ cursor: onRowClick ? "pointer" : undefined }}
            >
              {columns.map(col => (
                <td
                  key={col.key}
                  className={col.mono ? "vg-mono" : undefined}
                  style={{ textAlign: col.align ?? "left" }}
                >
                  {col.cell(row, idx)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
