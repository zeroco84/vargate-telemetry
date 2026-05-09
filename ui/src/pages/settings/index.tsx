import * as React from "react";
import { AppShell, PageHeader } from "../AppShell";
import { Button } from "../../design-system/components/Button";
import { Input } from "../../design-system/components/Input";
import { Card } from "../../design-system/components/Card";

type Tab = "tenant" | "integrations" | "billing";

const SettingsPage: React.FC = () => {
  const [tab, setTab] = React.useState<Tab>("tenant");
  return (
    <AppShell activeNav="settings" crumbs={[
      { label: "Acme Corp" },
      { label: "Settings" },
      { label: tab === "tenant" ? "Tenant" : tab === "integrations" ? "Integrations" : "Billing" },
    ]}>
      <PageHeader title="Settings" sub="Workspace · acme-prod" />

      <div style={{ display: "grid", gridTemplateColumns: "200px 1fr", gap: 32, alignItems: "flex-start" }}>
        <nav className="vg-stack" style={{ gap: 2, position: "sticky", top: 16 }}>
          {[
            { id: "tenant",       label: "Tenant" },
            { id: "integrations", label: "Integrations" },
            { id: "billing",      label: "Billing" },
          ].map(t => (
            <button
              key={t.id} type="button" onClick={() => setTab(t.id as Tab)}
              style={{
                textAlign: "left", border: "none",
                padding: "10px 12px", borderRadius: "var(--r-sm)",
                background: tab === t.id ? "var(--color-paper)" : "transparent",
                borderLeft: `2px solid ${tab === t.id ? "var(--color-ink)" : "transparent"}`,
                color: tab === t.id ? "var(--color-ink)" : "var(--color-ink-2)",
                fontFamily: "var(--font-sans)", fontSize: "var(--fs-base)",
                fontWeight: tab === t.id ? 500 : 400,
                cursor: "pointer",
              }}
            >
              {t.label}
            </button>
          ))}
        </nav>

        {tab === "tenant"       && <TenantPane />}
        {tab === "integrations" && <IntegrationsPane />}
        {tab === "billing"      && <BillingPane />}
      </div>
    </AppShell>
  );
};

const SettingRow: React.FC<{ label: string; help?: string; children: React.ReactNode }> = ({ label, help, children }) => (
  <div style={{ display: "grid", gridTemplateColumns: "260px 1fr", gap: 32, padding: "20px 0", borderBottom: "1px solid var(--color-line)" }}>
    <div>
      <div style={{ fontWeight: 500, color: "var(--color-ink)" }}>{label}</div>
      {help && <div style={{ fontSize: "var(--fs-sm)", color: "var(--color-ink-3)", marginTop: 4, lineHeight: 1.5 }}>{help}</div>}
    </div>
    <div>{children}</div>
  </div>
);

const TenantPane: React.FC = () => (
  <div>
    <h2 style={{ margin: "0 0 4px", fontSize: "var(--fs-xl)", fontWeight: 500, letterSpacing: "var(--ls-tighter)" }}>Tenant</h2>
    <p style={{ margin: 0, color: "var(--color-ink-3)", fontSize: "var(--fs-base)" }}>Workspace identity, residency, and retention.</p>

    <SettingRow label="Workspace name">
      <Input defaultValue="Acme Corp" />
    </SettingRow>
    <SettingRow label="Workspace ID">
      <Input mono defaultValue="ws_8e21" disabled />
    </SettingRow>
    <SettingRow label="Data residency" help="All telemetry and anchored hashes stay in this region. Cannot be changed once set.">
      <select className="vg-input" style={{ border: "1px solid var(--color-line-2)", borderRadius: "var(--r)", padding: "8px 12px", background: "var(--color-paper)" }}>
        <option>EU-WEST · Frankfurt</option>
        <option>US-EAST · Virginia</option>
        <option>UK · London</option>
      </select>
    </SettingRow>
    <SettingRow label="Retention" help="EU AI Act Art. 12 requires ≥ 6 months for high-risk systems. We recommend 12 months.">
      <select className="vg-input" style={{ border: "1px solid var(--color-line-2)", borderRadius: "var(--r)", padding: "8px 12px", background: "var(--color-paper)" }}>
        <option>6 months</option>
        <option>12 months</option>
        <option>24 months</option>
        <option>Indefinite</option>
      </select>
    </SettingRow>
    <SettingRow label="Anchor cadence" help="How often the integrity hash is published on-chain. Daily is recommended for cost; hourly is supported.">
      <select className="vg-input" style={{ border: "1px solid var(--color-line-2)", borderRadius: "var(--r)", padding: "8px 12px", background: "var(--color-paper)" }}>
        <option>Daily · 00:14 UTC</option>
        <option>Every 6h</option>
        <option>Hourly</option>
      </select>
    </SettingRow>

    <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 24 }}>
      <Button variant="ghost">Discard</Button>
      <Button variant="primary">Save changes</Button>
    </div>
  </div>
);

const IntegrationCard: React.FC<{ name: string; desc: string; status: "connected" | "disconnected" | "error" }> = ({ name, desc, status }) => {
  const map = {
    connected:    { label: "Connected",    cls: "vg-badge--anchored" },
    disconnected: { label: "Not connected", cls: "vg-badge--pending"  },
    error:        { label: "Error",        cls: "vg-badge--anomaly"  },
  } as const;
  return (
    <Card>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16 }}>
        <div>
          <div style={{ fontWeight: 500, fontSize: "var(--fs-md)" }}>{name}</div>
          <div style={{ color: "var(--color-ink-3)", fontSize: "var(--fs-sm)", marginTop: 4 }}>{desc}</div>
          <div style={{ marginTop: 12 }}>
            <span className={`vg-badge ${map[status].cls}`}>
              <span className="vg-badge__dot" />{map[status].label}
            </span>
          </div>
        </div>
        <Button variant="secondary" size="sm">{status === "connected" ? "Manage" : "Connect"}</Button>
      </div>
    </Card>
  );
};

const IntegrationsPane: React.FC = () => (
  <div>
    <h2 style={{ margin: "0 0 4px", fontSize: "var(--fs-xl)", fontWeight: 500, letterSpacing: "var(--ls-tighter)" }}>Integrations</h2>
    <p style={{ margin: "0 0 20px", color: "var(--color-ink-3)" }}>Sources, sinks, and the on-chain anchor target.</p>
    <div className="vg-grid" style={{ gridTemplateColumns: "1fr 1fr", gap: 16 }}>
      <IntegrationCard name="Anthropic management API" desc="Pull Claude usage events into the audit trail." status="connected" />
      <IntegrationCard name="On-chain anchor target"   desc="Publish daily integrity hashes. Currently: Ethereum L2." status="connected" />
      <IntegrationCard name="SIEM forwarder · Splunk"  desc="Mirror anomaly + compliance events to your SIEM."  status="connected" />
      <IntegrationCard name="SSO · Okta"               desc="SAML 2.0. Enforce login + group sync." status="connected" />
      <IntegrationCard name="SCIM provisioning"        desc="Provision actors automatically from your IdP." status="disconnected" />
      <IntegrationCard name="Slack alerts"             desc="Push danger-tier alerts to a channel." status="error" />
    </div>
  </div>
);

const BillingPane: React.FC = () => (
  <div>
    <h2 style={{ margin: "0 0 4px", fontSize: "var(--fs-xl)", fontWeight: 500, letterSpacing: "var(--ls-tighter)" }}>Billing</h2>
    <p style={{ margin: "0 0 20px", color: "var(--color-ink-3)" }}>Plan · seats · invoices.</p>

    <Card style={{ marginBottom: 16 } as React.CSSProperties}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 24 }}>
        <Stat label="Plan"    value="Enterprise" />
        <Stat label="Seats"   value="312 / 500" />
        <Stat label="MTD spend" value="$46,246" />
        <Stat label="Renews"  value="2026-09-01" />
      </div>
    </Card>

    <Card title="Invoices" sub="Last 12 months">
      <ul style={{ margin: 0, padding: 0, listStyle: "none" }}>
        {[
          { id: "INV-2026-04", date: "2026-04-01", amount: "$48,108.40", status: "Paid" },
          { id: "INV-2026-03", date: "2026-03-01", amount: "$44,920.10", status: "Paid" },
          { id: "INV-2026-02", date: "2026-02-01", amount: "$41,217.80", status: "Paid" },
        ].map(inv => (
          <li key={inv.id} style={{ display: "grid", gridTemplateColumns: "180px 140px 1fr 100px", gap: 16, padding: "12px 0", borderBottom: "1px dashed var(--color-line)", fontFamily: "var(--font-mono)", fontSize: "var(--fs-sm)" }}>
            <span>{inv.id}</span><span style={{ color: "var(--color-ink-3)" }}>{inv.date}</span><span>{inv.amount}</span>
            <span><span className="vg-badge vg-badge--anchored"><span className="vg-badge__dot" />{inv.status}</span></span>
          </li>
        ))}
      </ul>
    </Card>
  </div>
);

const Stat: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div>
    <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", letterSpacing: "var(--ls-wide)", textTransform: "uppercase", color: "var(--color-ink-3)" }}>{label}</div>
    <div style={{ fontSize: "var(--fs-xl)", fontWeight: 500, letterSpacing: "var(--ls-tighter)", marginTop: 4 }}>{value}</div>
  </div>
);

export default SettingsPage;
