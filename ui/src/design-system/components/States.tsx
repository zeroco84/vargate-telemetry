import * as React from "react";
import { IconAlert, IconInbox, IconSpinner } from "./icons";

export interface EmptyStateProps {
  title: React.ReactNode;
  body?: React.ReactNode;
  icon?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}

export const EmptyState: React.FC<EmptyStateProps> = ({ title, body, icon, action, className }) => (
  <div className={["vg-state", className].filter(Boolean).join(" ")}>
    <div className="vg-state__icon">{icon ?? <IconInbox />}</div>
    <h3 className="vg-state__title">{title}</h3>
    {body && <p className="vg-state__body">{body}</p>}
    {action}
  </div>
);

export interface ErrorStateProps {
  title?: React.ReactNode;
  body?: React.ReactNode;
  /** Optional action — typically a Retry button. */
  action?: React.ReactNode;
  className?: string;
}

export const ErrorState: React.FC<ErrorStateProps> = ({
  title = "Something went wrong",
  body,
  action,
  className,
}) => (
  <div className={["vg-state", "vg-state--error", className].filter(Boolean).join(" ")}>
    <div className="vg-state__icon"><IconAlert /></div>
    <h3 className="vg-state__title">{title}</h3>
    {body && <p className="vg-state__body">{body}</p>}
    {action}
  </div>
);

export interface LoadingStateProps {
  /** When true, render an inline spinner instead of skeleton rows. */
  inline?: boolean;
  /** Number of skeleton rows to show. */
  rows?: number;
  className?: string;
}

export const LoadingState: React.FC<LoadingStateProps> = ({
  inline = false,
  rows = 4,
  className,
}) => {
  if (inline) {
    return (
      <div className={["vg-state", className].filter(Boolean).join(" ")} style={{ borderStyle: "solid" }}>
        <div className="vg-state__icon"><IconSpinner /></div>
        <p className="vg-state__body" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", letterSpacing: "var(--ls-wide)", textTransform: "uppercase" }}>
          Loading…
        </p>
      </div>
    );
  }
  return (
    <div className={["vg-stack", className].filter(Boolean).join(" ")} style={{ gap: "var(--sp-2)" }}>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="vg-skel" style={{ width: `${60 + ((i * 13) % 35)}%` }} />
      ))}
    </div>
  );
};
