import * as React from "react";

export type ButtonVariant = "primary" | "secondary" | "danger" | "ghost" | "stamp";
export type ButtonSize = "sm" | "md" | "lg";

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Icon node placed before the label. Inherits currentColor. */
  leadingIcon?: React.ReactNode;
  /** Icon node placed after the label. */
  trailingIcon?: React.ReactNode;
}

/**
 * Primary button primitive. Use `variant="stamp"` only for integrity-action
 * CTAs (e.g. Anchor batch, Confirm reveal) — never for routine clicks.
 */
export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "primary", size = "md", leadingIcon, trailingIcon, className, children, ...rest },
  ref,
) {
  const cls = ["vg-btn", `vg-btn--${variant}`, `vg-btn--${size}`, className].filter(Boolean).join(" ");
  return (
    <button ref={ref} className={cls} {...rest}>
      {leadingIcon}
      {children}
      {trailingIcon}
    </button>
  );
});
