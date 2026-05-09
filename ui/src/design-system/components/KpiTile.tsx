import * as React from "react";

export interface KpiTileProps {
  label: string;
  value: React.ReactNode;
  /** Optional change indicator. Use `tone` to color it. */
  delta?: React.ReactNode;
  /** Color treatment for the delta line. */
  tone?: "neutral" | "up" | "down" | "warn";
  /** Sparkline values, normalized internally. Min 2 points. */
  spark?: number[];
  /** Color of the sparkline fill (defaults to ink-3). */
  sparkColor?: string;
  className?: string;
}

/**
 * Compact metric tile. Provide raw `spark` numbers; the component normalizes
 * and draws a filled area at the bottom. Pure presentation — no fetching.
 */
export const KpiTile: React.FC<KpiTileProps> = ({
  label,
  value,
  delta,
  tone = "neutral",
  spark,
  sparkColor,
  className,
}) => {
  const path = React.useMemo(() => buildSparkPath(spark), [spark]);
  const toneCls = tone !== "neutral" ? `vg-kpi__delta--${tone}` : "";

  return (
    <div className={["vg-kpi", className].filter(Boolean).join(" ")}>
      <div className="vg-kpi__label">{label}</div>
      <div className="vg-kpi__value">{value}</div>
      {delta && <div className={`vg-kpi__delta ${toneCls}`}>{delta}</div>}
      {path && (
        <svg
          className="vg-kpi__spark"
          viewBox="0 0 100 32"
          preserveAspectRatio="none"
          aria-hidden
        >
          <path d={path.area} fill={sparkColor ?? "var(--color-ink-4)"} fillOpacity={0.18} />
          <path d={path.line} stroke={sparkColor ?? "var(--color-ink-3)"} strokeWidth={1.2} fill="none" />
        </svg>
      )}
    </div>
  );
};

function buildSparkPath(spark?: number[]) {
  if (!spark || spark.length < 2) return null;
  const max = Math.max(...spark);
  const min = Math.min(...spark);
  const range = max - min || 1;
  const stepX = 100 / (spark.length - 1);
  const points = spark.map((v, i) => [i * stepX, 32 - ((v - min) / range) * 28 - 2] as const);
  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(2)} ${p[1].toFixed(2)}`).join(" ");
  const area = `${line} L100 32 L0 32 Z`;
  return { line, area };
}
