import { useEffect, useRef } from "react";
import { echarts } from "../charts";
import { axisLabel } from "../format";
import { formatTime } from "../time";
import type { XYAnalysis } from "../types";
import { cssVar } from "./TimePanels";

type P = [number, number, string, number | string];

const GROUPS: Record<string, { key: string | number; label: string; color: string }[]> = {
  tensile: [
    { key: "load", label: "Loading", color: "--series-1" },
    { key: "fracture", label: "Fracture", color: "--bad" },
  ],
  dsc: [
    { key: "heating", label: "Heating", color: "--series-1" },
    { key: "cooling", label: "Cooling", color: "--series-2" },
    { key: "isothermal", label: "Isothermal", color: "--faint" },
  ],
};

/** XY view in acquisition order; breaks at invalid pairs; segments coloured and named. */
export function XYChart(props: {
  title: string;
  analysis: XYAnalysis;
  tz: string;
  tzLabel: string;
  theme: string;
  markers: { name: string; x: number; y: number }[];
}) {
  const { title, analysis, tz, tzLabel, theme, markers } = props;
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!el.current) return;
    const c = echarts.init(el.current, undefined, { renderer: "canvas" });
    chart.current = c;
    const ro = new ResizeObserver(() => c.resize());
    ro.observe(el.current);
    return () => {
      ro.disconnect();
      c.dispose();
    };
  }, []);

  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    const fg = cssVar("--fg");
    const muted = cssVar("--muted");
    const gridLine = cssVar("--grid");
    const groups = GROUPS[analysis.kind] ?? [];
    const keyOf = (p: P | null): string | number | null =>
      p === null ? null : analysis.kind === "tensile" ? (p[3] === 3 ? "fracture" : "load") : p[3];
    const pts = analysis.points as (P | null)[];
    // One series per segment kind, in acquisition order. Points of other segments become breaks;
    // each segment also takes the point just before it so the trace stays continuous.
    const series = groups.map((g, i) => ({
      name: g.label,
      type: "line",
      showSymbol: false,
      connectNulls: false,
      lineStyle: { width: 1.8, color: cssVar(g.color) },
      itemStyle: { color: cssVar(g.color) },
      data: pts.map((p, k) =>
        p && (keyOf(p) === g.key || keyOf(pts[k + 1] ?? null) === g.key)
          ? [p[0], p[1], p[2]]
          : [null, null],
      ),
      markPoint:
        i === 0
          ? {
              symbol: "pin",
              symbolSize: 42,
              label: { fontSize: 10 },
              data: markers.map((m) => ({
                name: m.name,
                coord: [m.x, m.y],
                value: m.name,
              })),
              itemStyle: { color: cssVar("--accent") },
            }
          : undefined,
    }));
    const xu = analysis.x.unit ? ` (${analysis.x.unit})` : "";
    const yu = analysis.y.unit ? ` (${analysis.y.unit})` : "";
    c.setOption(
      {
        animation: false,
        aria: {
          enabled: true,
          label: {
            description: `${title}: ${analysis.y.label} versus ${analysis.x.label}.`,
          },
        },
        title: {
          text: title,
          left: 8,
          top: 2,
          textStyle: { fontSize: 13, color: fg, fontWeight: 600 },
        },
        toolbox: {
          right: 12,
          top: 0,
          iconStyle: { borderColor: muted },
          feature: {
            saveAsImage: {
              title: "Save PNG",
              name: title.replace(/[^A-Za-z0-9_-]+/g, "_"),
              pixelRatio: 2,
              backgroundColor: cssVar("--surface"),
            },
          },
        },
        legend: {
          type: "scroll",
          top: 26,
          left: 8,
          right: 40,
          textStyle: { color: fg },
        },
        grid: { left: 84, right: 28, top: 64, bottom: 56 },
        tooltip: {
          trigger: "item",
          formatter: (p: unknown) => {
            const v = (p as { value: P; seriesName: string }).value;
            if (!v || v[0] === null) return "";
            return `<b>${(p as { seriesName: string }).seriesName}</b><br/>${analysis.x.label}: ${v[0]}${xu}<br/>${analysis.y.label}: ${v[1]}${yu}<br/>${formatTime(v[2], tz, true, true)} ${tzLabel}`;
          },
        },
        xAxis: {
          type: "value",
          scale: true,
          ...(analysis.kind === "tensile"
            ? { min: (v: { min: number }) => (v.min > -0.02 ? 0 : v.min) }
            : {}),
          name: `${analysis.x.label}${xu}`,
          nameLocation: "middle",
          nameGap: 32,
          nameTextStyle: { color: muted },
          axisLabel: {
            color: muted,
            formatter: (v: number) => axisLabel(v, false),
          },
          splitLine: { lineStyle: { color: gridLine } },
        },
        yAxis: {
          type: "value",
          scale: true,
          name: `${analysis.y.label}${yu}${analysis.kind === "dsc" ? "  exo ↑" : ""}`,
          nameLocation: "middle",
          nameGap: 58,
          nameTextStyle: { color: muted },
          axisLabel: { color: muted },
          splitLine: { lineStyle: { color: gridLine } },
        },
        dataZoom: [
          {
            type: "inside",
            xAxisIndex: 0,
            filterMode: "none",
            zoomOnMouseWheel: "shift",
          },
        ],
        series,
      },
      { notMerge: true },
    );
  }, [analysis, title, tz, tzLabel, theme, markers]);

  return (
    <div
      ref={el}
      className="chart"
      style={{ height: 460 }}
      role="img"
      aria-label={`${title} XY chart`}
      data-testid="xy-chart"
    />
  );
}
