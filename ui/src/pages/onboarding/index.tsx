import * as React from "react";
import { Button } from "../../design-system/components/Button";
import { Input } from "../../design-system/components/Input";
import { IconCheck } from "../../design-system/components/icons";

interface Step {
  id: string;
  num: string;
  title: string;
  body: string;
  done: boolean;
}

const initialSteps: Step[] = [
  { id: "key",        num: "01", title: "Connect Anthropic management API", body: "We use a read-only key to pull events. The key never leaves your tenant; we hash and discard the payload after anchoring.", done: false },
  { id: "residency",  num: "02", title: "Choose data residency",            body: "All telemetry and anchored hashes stay in this region. This cannot be changed after the first anchor.", done: false },
  { id: "frameworks", num: "03", title: "Pick compliance frameworks",        body: "Pre-load policy packs for the regulations you operate under. You can add more later.", done: false },
  { id: "anchor",     num: "04", title: "Verify first anchor",               body: "We'll anchor a test batch on-chain and walk you through verifying the proof. Takes ~2 minutes.", done: false },
];

const OnboardingPage: React.FC = () => {
  const [steps, setSteps] = React.useState(initialSteps);
  const [active, setActive] = React.useState("key");

  const completeStep = (id: string) => {
    setSteps(s => s.map(x => x.id === id ? { ...x, done: true } : x));
    const idx = steps.findIndex(x => x.id === id);
    if (idx >= 0 && idx < steps.length - 1) setActive(steps[idx + 1].id);
  };

  const allDone = steps.every(s => s.done);

  return (
    <div style={{ minHeight: "100vh", background: "var(--color-paper-2)", display: "flex", justifyContent: "center", padding: "64px 24px" }}>
      <div style={{ width: "100%", maxWidth: 720 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 32 }}>
          <div style={{ width: 22, height: 22, background: "var(--color-indigo)", borderRadius: "var(--r-sm)" }} aria-hidden />
          <span style={{ fontWeight: 500, letterSpacing: "var(--ls-tight)" }}>Vargate</span>
          <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", letterSpacing: "var(--ls-widest)", color: "var(--color-ink-3)", textTransform: "uppercase", borderLeft: "1px solid var(--color-line-2)", paddingLeft: 10 }}>Telemetry</span>
        </div>

        <h1 style={{ fontSize: "var(--fs-3xl)", fontWeight: 500, letterSpacing: "var(--ls-tightest)", margin: "0 0 8px" }}>
          Set up your audit trail.
        </h1>
        <p style={{ color: "var(--color-ink-2)", fontSize: "var(--fs-lg)", lineHeight: 1.45, margin: "0 0 32px", maxWidth: 580 }}>
          Four steps. About five minutes. By the end you'll have an independent, hash-chained record of how your team uses Claude.
        </p>

        <ProgressTrack steps={steps} active={active} />

        <div className="vg-stack" style={{ gap: 12 }}>
          {steps.map(s => (
            <StepCard
              key={s.id} step={s}
              isActive={active === s.id}
              onActivate={() => setActive(s.id)}
              onComplete={() => completeStep(s.id)}
            />
          ))}
        </div>

        {allDone && (
          <div style={{ marginTop: 32, padding: "20px 24px", border: "1px solid var(--color-anchored)", background: "var(--color-anchored-tint)", borderRadius: "var(--r)", display: "flex", alignItems: "center", gap: 16, justifyContent: "space-between" }}>
            <div>
              <div style={{ fontWeight: 500 }}>You're set up.</div>
              <div style={{ color: "var(--color-ink-2)", fontSize: "var(--fs-sm)", marginTop: 4 }}>First anchor confirmed. The audit trail is live.</div>
            </div>
            <Button variant="primary">Open Telemetry →</Button>
          </div>
        )}
      </div>
    </div>
  );
};

const ProgressTrack: React.FC<{ steps: Step[]; active: string }> = ({ steps, active }) => (
  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 32 }}>
    {steps.map((s, i) => {
      const state: "done" | "current" | "pending" = s.done ? "done" : (s.id === active ? "current" : "pending");
      return (
        <React.Fragment key={s.id}>
          <div style={{
            width: 28, height: 28, borderRadius: "50%",
            display: "inline-flex", alignItems: "center", justifyContent: "center",
            fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)",
            background: state === "done" ? "var(--color-anchored)" : state === "current" ? "var(--color-ink)" : "var(--color-paper)",
            color:      state === "pending" ? "var(--color-ink-3)" : "var(--color-paper)",
            border: `1px solid ${state === "pending" ? "var(--color-line-2)" : "transparent"}`,
          }}>
            {state === "done" ? <IconCheck /> : s.num}
          </div>
          {i < steps.length - 1 && <div style={{ flex: 1, height: 1, background: state === "done" ? "var(--color-anchored)" : "var(--color-line)" }} />}
        </React.Fragment>
      );
    })}
  </div>
);

const StepCard: React.FC<{ step: Step; isActive: boolean; onActivate: () => void; onComplete: () => void }> = ({ step, isActive, onActivate, onComplete }) => {
  const open = isActive && !step.done;
  return (
    <div
      style={{
        background: "var(--color-paper)",
        border: `1px solid ${open ? "var(--color-ink)" : "var(--color-line)"}`,
        borderRadius: "var(--r)",
        padding: open ? "20px 24px" : "16px 20px",
        transition: "border-color var(--dur) var(--ease)",
      }}
    >
      <button type="button" onClick={onActivate}
        style={{ display: "grid", gridTemplateColumns: "32px 1fr auto", gap: 16, width: "100%", alignItems: "center", background: "transparent", border: "none", padding: 0, textAlign: "left", cursor: "pointer", fontFamily: "inherit", color: "inherit" }}
      >
        <div style={{
          width: 28, height: 28, borderRadius: "50%",
          display: "inline-flex", alignItems: "center", justifyContent: "center",
          fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)",
          background: step.done ? "var(--color-anchored)" : "var(--color-paper-3)",
          color: step.done ? "var(--color-paper)" : "var(--color-ink-2)",
        }}>
          {step.done ? <IconCheck /> : step.num}
        </div>
        <div>
          <div style={{ fontSize: "var(--fs-md)", fontWeight: 500, letterSpacing: "var(--ls-tight)", color: step.done ? "var(--color-ink-3)" : "var(--color-ink)" }}>{step.title}</div>
          {!open && !step.done && (
            <div style={{ fontSize: "var(--fs-sm)", color: "var(--color-ink-3)", marginTop: 4 }}>{step.body}</div>
          )}
        </div>
        <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-3)", letterSpacing: "var(--ls-wide)", textTransform: "uppercase" }}>
          {step.done ? "DONE" : open ? "" : "·"}
        </div>
      </button>

      {open && (
        <div style={{ marginTop: 16, paddingLeft: 48 }}>
          <p style={{ margin: "0 0 16px", color: "var(--color-ink-2)", lineHeight: 1.55 }}>{step.body}</p>
          {step.id === "key" && <Input label="Anthropic API key" mono placeholder="sk-ant-•••••••••••••••••••••••" />}
          {step.id === "residency" && (
            <div className="vg-row">
              {["EU-WEST", "US-EAST", "UK"].map(r => (
                <button key={r} type="button" style={{ padding: "10px 16px", border: "1px solid var(--color-line-2)", borderRadius: "var(--r)", background: "var(--color-paper)", fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", letterSpacing: "var(--ls-wide)", cursor: "pointer" }}>{r}</button>
              ))}
            </div>
          )}
          {step.id === "frameworks" && (
            <div className="vg-row">
              {["EU AI Act", "SOX", "HIPAA", "ISO 42001", "GDPR"].map(f => (
                <label key={f} style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "6px 10px", border: "1px solid var(--color-line-2)", borderRadius: "var(--r)", cursor: "pointer" }}>
                  <input type="checkbox" defaultChecked={f === "EU AI Act" || f === "GDPR"} style={{ accentColor: "var(--color-indigo)" }} />
                  <span style={{ fontSize: "var(--fs-sm)" }}>{f}</span>
                </label>
              ))}
            </div>
          )}
          {step.id === "anchor" && (
            <div style={{ padding: "12px 14px", background: "var(--color-paper-2)", border: "1px dashed var(--color-line-2)", borderRadius: "var(--r)", fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--color-ink-2)" }}>
              Test batch ready · 142 events · root <span style={{ color: "var(--color-ink)" }}>0xc93a…7e21</span>
            </div>
          )}
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 16, gap: 8 }}>
            <Button variant="ghost" onClick={onComplete}>Skip for now</Button>
            <Button variant="primary" onClick={onComplete}>Mark complete</Button>
          </div>
        </div>
      )}
    </div>
  );
};

export default OnboardingPage;
