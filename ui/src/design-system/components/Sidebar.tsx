import * as React from "react";
import { IconChevronRight } from "./icons";

export interface SidebarItem {
  id: string;
  label: React.ReactNode;
  icon?: React.ReactNode;
  count?: number | string;
  /** Active when the current route matches. */
  active?: boolean;
  href?: string;
  onClick?: () => void;
}

export interface SidebarGroup {
  id: string;
  title: string;
  items: SidebarItem[];
  defaultOpen?: boolean;
}

export interface SidebarProps {
  /** Workspace name (e.g. "Acme Corp"). */
  workspace: string;
  /** Product label (e.g. "Telemetry"). */
  product: string;
  groups: SidebarGroup[];
  /** Override the indigo Vargate mark with custom node. */
  brandMark?: React.ReactNode;
  className?: string;
}

/**
 * Application sidebar for Vargate Telemetry. Indigo mark + workspace name +
 * monospace product label, then collapsible link groups. Groups remember
 * their open/closed state via internal state — promote to controlled if the
 * host needs to persist it.
 */
export const Sidebar: React.FC<SidebarProps> = ({
  workspace,
  product,
  groups,
  brandMark,
  className,
}) => {
  const [openMap, setOpenMap] = React.useState<Record<string, boolean>>(
    () => Object.fromEntries(groups.map(g => [g.id, g.defaultOpen ?? true])),
  );

  return (
    <nav className={["vg-side", className].filter(Boolean).join(" ")}>
      <div className="vg-side__brand">
        {brandMark ?? <div className="vg-side__brand-mark" aria-hidden />}
        <div>
          <div className="vg-side__brand-text">{workspace}</div>
        </div>
        <div className="vg-side__brand-product">{product}</div>
      </div>
      <div className="vg-side__nav">
        {groups.map(g => {
          const isOpen = openMap[g.id] ?? true;
          return (
            <div key={g.id} className="vg-side__group" data-open={isOpen}>
              <button
                type="button"
                className="vg-side__group-head"
                aria-expanded={isOpen}
                onClick={() => setOpenMap(m => ({ ...m, [g.id]: !isOpen }))}
              >
                <span>{g.title}</span>
                <span className="vg-side__group-chev"><IconChevronRight /></span>
              </button>
              <div className="vg-side__items">
                {g.items.map(item => (
                  <a
                    key={item.id}
                    className="vg-side__item"
                    href={item.href ?? "#"}
                    aria-current={item.active ? "page" : undefined}
                    onClick={item.onClick ? e => { e.preventDefault(); item.onClick!(); } : undefined}
                  >
                    {item.icon}
                    <span>{item.label}</span>
                    {item.count != null && <span className="vg-side__item-count">{item.count}</span>}
                  </a>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </nav>
  );
};
