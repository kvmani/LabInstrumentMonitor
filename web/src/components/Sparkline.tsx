import type { Series } from "../types";

/**
 * Small trend for a card: SVG, no chart library. Gaps and bad samples break the line;
 * bucketed data plots the bucket mean. Log scale for positive quantities such as pressure.
 */
export function Sparkline({
  series,
  log = false,
  label,
}: {
  series: Series | undefined;
  log?: boolean;
  label: string;
}) {
  if (!series) return <div className="spark" aria-hidden="true" />;
  const start = Date.parse(series.range.start);
  const end = Date.parse(series.range.end);
  const pts: [number, number | null][] =
    series.mode === "raw"
      ? (series.points ?? []).map((p) => [Date.parse(p[0]), p[1]])
      : (series.buckets ?? []).map((b) => [Date.parse(b.t), b.mean]);
  for (const g of series.gaps) pts.push([(Date.parse(g.start) + Date.parse(g.end)) / 2, null]);
  pts.sort((a, b) => a[0] - b[0]);
  const tf = (v: number) => (log ? Math.log10(Math.max(v, 1e-12)) : v);
  const vals = pts
    .map((p) => p[1])
    .filter((v): v is number => v !== null)
    .map(tf);
  if (vals.length < 2)
    return (
      <div className="spark muted sub" role="img" aria-label={`${label}: not enough data`}>
        no recent data
      </div>
    );
  let lo = Math.min(...vals);
  let hi = Math.max(...vals);
  if (hi - lo < 1e-9) {
    lo -= 1;
    hi += 1;
  }
  const W = 300;
  const H = 56;
  const x = (t: number) => ((t - start) / Math.max(end - start, 1)) * W;
  const y = (v: number) => H - 4 - ((tf(v) - lo) / (hi - lo)) * (H - 8);
  const segments: string[] = [];
  let cur: string[] = [];
  for (const [t, v] of pts) {
    if (v === null) {
      if (cur.length) segments.push(cur.join(" "));
      cur = [];
    } else cur.push(`${x(t).toFixed(1)},${y(v).toFixed(1)}`);
  }
  if (cur.length) segments.push(cur.join(" "));
  return (
    <svg
      className="spark"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`${label}, last 15 minutes`}
    >
      {segments.map((s, i) => {
        const first = s.split(" ")[0]!.split(",")[0];
        const last = s.split(" ").at(-1)!.split(",")[0];
        return (
          <g key={i}>
            <polygon className="area" points={`${first},${H} ${s} ${last},${H}`} />
            <polyline className="line" points={s} />
          </g>
        );
      })}
    </svg>
  );
}
