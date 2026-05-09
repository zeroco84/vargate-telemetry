import * as React from "react";

export interface CardProps extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  /** Title rendered in the header. Header omitted entirely if absent. */
  title?: React.ReactNode;
  /** Mono-styled subtitle / kicker on the right side of the header. */
  sub?: React.ReactNode;
  /** Footer node. Footer omitted entirely if absent. */
  footer?: React.ReactNode;
  /** Tighter padding for dense layouts (audit logs, KPI rows). */
  dense?: boolean;
  /** Optional toolbar rendered to the right of the title. */
  actions?: React.ReactNode;
}

/**
 * Surface primitive. Use `title`/`sub`/`actions` for the standard header
 * and `footer` for actions-bar treatments. Set `dense` for audit / table cards.
 */
export const Card: React.FC<CardProps> = ({
  title,
  sub,
  footer,
  actions,
  dense,
  className,
  children,
  ...rest
}) => {
  const cls = ["vg-card", dense && "vg-card--dense", className].filter(Boolean).join(" ");
  return (
    <div className={cls} {...rest}>
      {(title || sub || actions) && (
        <div className="vg-card__header">
          <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
            {title && <h3 className="vg-card__title">{title}</h3>}
            {sub && <span className="vg-card__sub">{sub}</span>}
          </div>
          {actions && <div className="vg-row" style={{ gap: 8 }}>{actions}</div>}
        </div>
      )}
      <div className="vg-card__body">{children}</div>
      {footer && <div className="vg-card__footer">{footer}</div>}
    </div>
  );
};
