import * as React from "react";
import { IconClose } from "./icons";

export interface DrillCrumb {
  label: React.ReactNode;
  onClick?: () => void;
}

export interface DrillThroughProps {
  open: boolean;
  onClose: () => void;
  /** Trail of where we came from. Last entry is rendered emphasized. */
  crumbs: DrillCrumb[];
  children: React.ReactNode;
  /** Optional footer node — actions, anchor button, etc. */
  footer?: React.ReactNode;
  width?: number;
}

/**
 * Right-side slide-over for drill-through detail. Used to inspect a row from
 * a table without losing its place. Press Esc or click the scrim to close.
 *
 * Expects a portal target — appends to document.body. SSR-safe (renders null
 * before mount).
 */
export const DrillThrough: React.FC<DrillThroughProps> = ({
  open,
  onClose,
  crumbs,
  children,
  footer,
  width = 540,
}) => {
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => { setMounted(true); }, []);

  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!mounted) return null;

  return (
    <>
      {open && (
        <div
          className="vg-drill__scrim"
          data-open={open}
          onClick={onClose}
          aria-hidden
        />
      )}
      <aside
        className="vg-drill"
        data-open={open}
        style={{ width }}
        role="dialog"
        aria-modal="true"
      >
        <div className="vg-drill__head">
          <div className="vg-drill__crumbs">
            {crumbs.map((c, i) => (
              <React.Fragment key={i}>
                {i > 0 && <span aria-hidden>›</span>}
                {i === crumbs.length - 1 ? (
                  <em>{c.label}</em>
                ) : c.onClick ? (
                  <button
                    type="button"
                    onClick={c.onClick}
                    style={{ background: "none", border: "none", padding: 0, color: "inherit", cursor: "pointer", font: "inherit", letterSpacing: "inherit" }}
                  >
                    {c.label}
                  </button>
                ) : <span>{c.label}</span>}
              </React.Fragment>
            ))}
          </div>
          <button type="button" className="vg-drill__close" onClick={onClose} aria-label="Close">
            <IconClose />
          </button>
        </div>
        <div className="vg-drill__body">{children}</div>
        {footer && (
          <div style={{ padding: "14px 20px", borderTop: "1px solid var(--color-line)", background: "var(--color-paper-2)" }}>
            {footer}
          </div>
        )}
      </aside>
    </>
  );
};
