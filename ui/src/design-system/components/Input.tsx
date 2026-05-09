import * as React from "react";
import { IconEye, IconEyeOff } from "./icons";

export type InputType = "text" | "password" | "email";

export interface InputProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "type" | "size"> {
  /** Field label rendered above the control. */
  label?: string;
  /** Field type. `password` shows a reveal toggle. */
  type?: InputType;
  /** Helper text under the field (or error message when `error` is true). */
  hint?: string;
  /** When true, applies error styling and treats `hint` as the error message. */
  error?: boolean;
  /** Renders the control with a monospace font — for hashes / IDs. */
  mono?: boolean;
  /** Optional id; auto-generated if omitted. */
  id?: string;
}

let inputCount = 0;
const nextId = () => `vg-input-${++inputCount}`;

/**
 * Text input with optional label, error state, and a built-in reveal toggle
 * for `type="password"`. Purely presentational — no validation logic.
 */
export const Input = React.forwardRef<HTMLInputElement, InputProps>(function Input(
  { label, type = "text", hint, error, mono, disabled, className, id: idProp, ...rest },
  ref,
) {
  const [revealed, setRevealed] = React.useState(false);
  const id = React.useMemo(() => idProp ?? nextId(), [idProp]);
  const realType = type === "password" && revealed ? "text" : type;

  const wrapCls = [
    "vg-field",
    error && "vg-field--error",
    disabled && "vg-field--disabled",
    className,
  ].filter(Boolean).join(" ");

  return (
    <div className={wrapCls}>
      {label && <label className="vg-field__label" htmlFor={id}>{label}</label>}
      <div className="vg-field__control">
        <input
          ref={ref}
          id={id}
          type={realType}
          disabled={disabled}
          aria-invalid={error || undefined}
          className={["vg-input", mono && "vg-input--mono"].filter(Boolean).join(" ")}
          {...rest}
        />
        {type === "password" && (
          <button
            type="button"
            tabIndex={-1}
            className="vg-field__icon-btn"
            aria-label={revealed ? "Hide password" : "Show password"}
            onClick={() => setRevealed(v => !v)}
          >
            {revealed ? <IconEyeOff /> : <IconEye />}
          </button>
        )}
      </div>
      {hint && <div className="vg-field__msg">{hint}</div>}
    </div>
  );
});
