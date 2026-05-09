import * as React from "react";
import { IconChevronDown } from "./icons";

export interface TopbarCrumb {
  label: React.ReactNode;
  href?: string;
  onClick?: () => void;
}

export interface TopbarProps {
  crumbs: TopbarCrumb[];
  /** Data-residency / environment indicator (e.g. "EU-WEST · PROD"). */
  env?: { label: string; region?: "us" | "eu" | "neutral" };
  /** Tenant switcher. Click handler is up to the caller (open a popover). */
  tenant?: { name: string; meta?: string; onClick?: () => void };
  /** User avatar initials. */
  user?: { initials: string; onClick?: () => void };
  /** Right-side custom slot rendered before the user avatar. */
  actions?: React.ReactNode;
  className?: string;
}

/**
 * Application topbar: crumbs left, env + tenant + actions + avatar right.
 * Region indicator is mandatory for EU-resident customers — defaults to
 * neutral styling if not supplied.
 */
export const Topbar: React.FC<TopbarProps> = ({
  crumbs,
  env,
  tenant,
  user,
  actions,
  className,
}) => {
  const envCls = env?.region === "us" ? "vg-top__env--us"
    : env?.region === "eu" ? "vg-top__env--eu"
    : "";
  return (
    <header className={["vg-top", className].filter(Boolean).join(" ")}>
      <nav className="vg-top__crumbs" aria-label="Breadcrumb">
        {crumbs.map((c, i) => (
          <React.Fragment key={i}>
            {i > 0 && <span aria-hidden> / </span>}
            {i === crumbs.length - 1 ? (
              <em>{c.label}</em>
            ) : c.onClick || c.href ? (
              <a
                href={c.href ?? "#"}
                onClick={c.onClick ? e => { e.preventDefault(); c.onClick!(); } : undefined}
                style={{ color: "inherit", textDecoration: "none" }}
              >
                {c.label}
              </a>
            ) : <span>{c.label}</span>}
          </React.Fragment>
        ))}
      </nav>
      <div className="vg-top__spacer" />
      {env && <span className={`vg-top__env ${envCls}`}>{env.label}</span>}
      {tenant && (
        <button type="button" className="vg-top__tenant" onClick={tenant.onClick}>
          <span>{tenant.name}</span>
          {tenant.meta && <span className="vg-top__tenant-meta">{tenant.meta}</span>}
          <IconChevronDown />
        </button>
      )}
      {actions}
      {user && (
        <button type="button" className="vg-top__user" onClick={user.onClick} aria-label="User menu">
          {user.initials}
        </button>
      )}
    </header>
  );
};
