import * as React from "react";
import { IconAlert, IconChevronRight, IconInfo, IconShieldX } from "./icons";

export type AlertSeverity = "info" | "warning" | "danger";

export interface AlertRowProps {
  severity: AlertSeverity;
  title: React.ReactNode;
  body: React.ReactNode;
  /** Mono timestamp shown on the right of the head. */
  timestamp: string;
  /** Pill-style source tag rendered next to the title. */
  source?: string;
  /** Controlled open state. If omitted, the row manages its own state. */
  open?: boolean;
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  className?: string;
}

const ICONS: Record<AlertSeverity, React.FC> = {
  info: IconInfo,
  warning: IconAlert,
  danger: IconShieldX,
};

/**
 * Severity-coded row with expand-on-click body. Use for compliance alerts and
 * anomaly notifications; `severity` drives icon + color treatment.
 */
export const AlertRow: React.FC<AlertRowProps> = ({
  severity,
  title,
  body,
  timestamp,
  source,
  open: openProp,
  defaultOpen,
  onOpenChange,
  className,
}) => {
  const [internalOpen, setInternalOpen] = React.useState(defaultOpen ?? false);
  const isOpen = openProp ?? internalOpen;
  const Icon = ICONS[severity];

  const toggle = () => {
    const next = !isOpen;
    if (openProp === undefined) setInternalOpen(next);
    onOpenChange?.(next);
  };

  return (
    <div
      className={[
        "vg-alert",
        `vg-alert--${severity}`,
        isOpen && "vg-alert--open",
        className,
      ].filter(Boolean).join(" ")}
    >
      <button
        type="button"
        className="vg-alert__head"
        aria-expanded={isOpen}
        onClick={toggle}
      >
        <span className="vg-alert__icon"><Icon /></span>
        <span>
          <span className="vg-alert__title">{title}</span>
          {source && (
            <span className="vg-alert__sub">
              <span className="vg-badge vg-badge--info" style={{ padding: "2px 6px" }}>{source}</span>
            </span>
          )}
        </span>
        <span className="vg-alert__time">{timestamp}</span>
        <span className="vg-alert__chev"><IconChevronRight /></span>
      </button>
      {isOpen && <div className="vg-alert__body">{body}</div>}
    </div>
  );
};
