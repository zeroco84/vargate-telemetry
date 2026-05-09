import * as React from "react";
import { IconLock, IconUnlock, IconCheck, IconX } from "./icons";

export type RedactionState = "locked" | "confirming" | "revealing" | "revealed";

export interface RedactionToggleProps {
  /** The actual sensitive value. Only displayed when state === "revealed". */
  value: React.ReactNode;
  /** Reason text shown in the confirmation step (audit log will record this). */
  reasonPrompt?: string;
  /** Called when reveal is confirmed. The receipt id should be a hash or row id. */
  onReveal?: (reason: string) => void | Promise<void>;
  /** Receipt shown after reveal — usually a hash + actor. */
  receipt?: React.ReactNode;
  className?: string;
  /** Override starting state (mostly for stories). */
  initial?: RedactionState;
}

/**
 * Field-level redaction toggle. Three-step interaction:
 *   1. Locked       — value masked
 *   2. Confirming   — user must click "Confirm reveal" (this writes an audit row)
 *   3. Revealed     — value visible, receipt shown
 *
 * The seal "breaks" via clip-path animation between steps 2 and 3.
 *
 * This is intentionally NOT a drop-in for `<input type=password>`: revealing a
 * value is itself an audited event in Vargate, so the UX makes the cost of
 * looking visible and recoverable.
 */
export const RedactionToggle: React.FC<RedactionToggleProps> = ({
  value,
  reasonPrompt = "Confirm reveal — this action will be logged.",
  onReveal,
  receipt,
  className,
  initial = "locked",
}) => {
  const [state, setState] = React.useState<RedactionState>(initial);

  const reveal = async () => {
    setState("revealing");
    await Promise.resolve(onReveal?.("manual"));
    // Animation duration matches --dur-slow (280ms).
    window.setTimeout(() => setState("revealed"), 260);
  };

  return (
    <div className={["vg-redact", className].filter(Boolean).join(" ")} data-state={state}>
      <div className="vg-redact__shell">
        <span className="vg-redact__icon">
          {state === "revealed" ? <IconUnlock /> : <IconLock />}
        </span>
        <span className="vg-redact__content">
          {state === "confirming" ? reasonPrompt
            : state === "revealed" || state === "revealing" ? value
            : <span aria-hidden>{value}</span>}
        </span>
        {state === "locked" && (
          <button type="button" className="vg-redact__btn" onClick={() => setState("confirming")}>
            Reveal
          </button>
        )}
        {state === "confirming" && (
          <>
            <button type="button" className="vg-redact__btn vg-redact__btn--cancel" onClick={() => setState("locked")}>
              <IconX /> Cancel
            </button>
            <button type="button" className="vg-redact__btn vg-redact__btn--confirm" onClick={reveal}>
              <IconCheck /> Confirm
            </button>
          </>
        )}
      </div>
      {state === "revealed" && receipt && (
        <span className="vg-redact__receipt">
          <IconCheck /> Reveal recorded: <strong>{receipt}</strong>
        </span>
      )}
    </div>
  );
};
