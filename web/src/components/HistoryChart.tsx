import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import {
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  TitleComponent,
  ToolboxComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { Series } from "../types";
import { formatDuration, formatTime } from "../time";

echarts.use([
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  DataZoomComponent,
  AriaComponent,
  TitleComponent,
  ToolboxComponent,
  CanvasRenderer,
]);

export interface YScale {
  mode: "auto" | "fixed";
  min: number;
  max: number;
}

type Pt = [number, number | null];

const MIN_SPAN: Record<string, number> = { temperature: 2 }; // minimum engineering span (°C)

function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

/** Points for a line, with a null inserted inside every gap so the trace breaks there. */
function linePoints(s: Series): Pt[] {
  const pts: Pt[] = [];
  if (s.mode === "raw") {
    for (const p of s.points ?? []) pts.push([Date.parse(p[0]), p[1]]);
  } else {
    for (const b of s.buckets ?? []) pts.push([Date.parse(b.t) + (s.bucket_s ?? 0) * 500, b.mean]);
  }
  for (const g of s.gaps) {
    const a = Date.parse(g.start);
    const b = Date.parse(g.end);
    pts.push([a + (b - a) / 2, null]);
  }
  return pts.sort((x, y) => x[0] - y[0]);
}

function stepPoints(s: Series): Pt[] {
  if (s.mode === "raw") return linePoints(s);
  const pts: Pt[] = (s.buckets ?? []).map((b) => [Date.parse(b.t), b.last]);
  for (const g of s.gaps) pts.push([(Date.parse(g.start) + Date.parse(g.end)) / 2, null]);
  return pts.sort((x, y) => x[0] - y[0]);
}

function band(s: Series): { lo: Pt[]; span: Pt[] } {
  const lo: Pt[] = [];
  const span: Pt[] = [];
  const w = (s.bucket_s ?? 0) * 500;
  for (const b of s.buckets ?? []) {
    const t = Date.parse(b.t) + w;
    lo.push([t, b.min]);
    span.push([t, b.min === null || b.max === null ? null : b.max - b.min]);
  }
  for (const g of s.gaps) {
    const t = (Date.parse(g.start) + Date.parse(g.end)) / 2;
    lo.push([t, null]);
    span.push([t, null]);
  }
  lo.sort((x, y) => x[0] - y[0]);
  span.sort((x, y) => x[0] - y[0]);
  return { lo, span };
}

export function HistoryChart(props: {
  title: string;
  measured: Series | undefined;
  setpoint: Series | undefined;
  state: Series | undefined;
  start: number;
  end: number;
  tz: string;
  tzLabel: string;
  yScale: YScale;
  onZoom: (start: number, end: number) => void;
}) {
  const { title, measured, setpoint, state, start, end, tz, tzLabel, yScale, onZoom } = props;
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const zoomRef = useRef(onZoom);
  zoomRef.current = onZoom;
  const rangeRef = useRef({ start, end });
  rangeRef.current = { start, end };

  useEffect(() => {
    if (!el.current) return;
    const c = echarts.init(el.current, undefined, { renderer: "canvas" });
    chart.current = c;
    let timer: number | undefined;
    c.on("datazoom", () => {
      // user zoom/pan: hand the window to the page, which pauses live follow and refetches
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        const opt = c.getOption() as { dataZoom?: { start?: number; end?: number }[] };
        const dz = opt.dataZoom?.[0];
        if (!dz || dz.start === undefined || dz.end === undefined) return;
        if (dz.start <= 0.01 && dz.end >= 99.99) return;
        const { start: s, end: e } = rangeRef.current;
        const a = s + ((e - s) * dz.start) / 100;
        const b = s + ((e - s) * dz.end) / 100;
        if (b - a >= 10_000) zoomRef.current(Math.round(a), Math.round(b));
      }, 350);
    });
    const ro = new ResizeObserver(() => c.resize());
    ro.observe(el.current);
    return () => {
      window.clearTimeout(timer);
      ro.disconnect();
      c.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    const c = chart.current;
    if (!c || !measured) return;
    const unit = measured.channel.unit;
    const prec = measured.channel.display_precision;
    const minSpan = MIN_SPAN[measured.channel.quantity] ?? 0;
    const fg = css("--fg");
    const muted = css("--muted");
    const gridLine = css("--grid");
    const gapFill = css("--gap");
    const bad = css("--bad");
    const pvColor = css("--series-1");
    const spColor = css("--series-2");
    const stColor = css("--series-3");
    const labels = state?.channel.enum_labels ?? {};
    const codes = Object.keys(labels).map(Number);

    const gapAreas = measured.gaps.map((g) => [
      {
        xAxis: Date.parse(g.start),
        name: g.open ? `No data since ${formatTime(g.start, tz, false)}` : `No data · ${formatDuration(g.duration_s)}`,
      },
      { xAxis: Date.parse(g.end) },
    ]);
    const badLines =
      measured.mode === "raw"
        ? (measured.points ?? [])
            .filter((p) => p[2] === "bad")
            .slice(0, 200)
            .map((p) => ({ xAxis: Date.parse(p[0]), name: `bad: ${(p[3] ?? "").replace(/_/g, " ")}` }))
        : (measured.buckets ?? [])
            .filter((b) => b.bad_count > 0)
            .slice(0, 200)
            .map((b) => ({ xAxis: Date.parse(b.t), name: `bad ×${b.bad_count}` }));
    const eventLines = measured.events
      .filter((e) => e.type !== "acquisition_started" && e.type !== "acquisition_stopped")
      .slice(0, 50)
      .map((e) => ({ xAxis: Date.parse(e.at), name: e.type.replace(/_/g, " ") }));

    const autoMin = (v: { min: number; max: number }) => {
      const span = Math.max(v.max - v.min, minSpan);
      const mid = (v.max + v.min) / 2;
      return Number((mid - span / 2 - span * 0.05).toFixed(prec));
    };
    const autoMax = (v: { min: number; max: number }) => {
      const span = Math.max(v.max - v.min, minSpan);
      const mid = (v.max + v.min) / 2;
      return Number((mid + span / 2 + span * 0.05).toFixed(prec));
    };

    const series: echarts.EChartsCoreOption[] = [];
    if (measured.mode === "bucket") {
      const { lo, span } = band(measured);
      series.push(
        { name: "min", type: "line", data: lo, stack: "band", symbol: "none", lineStyle: { opacity: 0 }, silent: true, connectNulls: false, xAxisIndex: 0, yAxisIndex: 0, tooltip: { show: false } },
        { name: `${measured.channel.name} min–max`, type: "line", data: span, stack: "band", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: pvColor, opacity: 0.25 }, connectNulls: false, xAxisIndex: 0, yAxisIndex: 0 },
      );
    }
    series.push({
      name: measured.mode === "bucket" ? `${measured.channel.name} (mean)` : measured.channel.name,
      type: "line",
      data: linePoints(measured),
      showSymbol: measured.mode === "raw" && (measured.points?.length ?? 0) < 400,
      symbolSize: 4,
      sampling: undefined,
      smooth: false,
      connectNulls: false,
      lineStyle: { width: 1.5, color: pvColor },
      itemStyle: { color: pvColor },
      xAxisIndex: 0,
      yAxisIndex: 0,
      markArea: { silent: true, itemStyle: { color: gapFill }, label: { color: muted, fontSize: 11, position: "insideTop" }, data: gapAreas },
      markLine: {
        silent: true,
        symbol: "none",
        data: [
          ...badLines.map((d) => ({ ...d, lineStyle: { color: bad, type: "dashed", width: 1 }, label: { show: badLines.length < 12, formatter: "{b}", color: bad, fontSize: 10 } })),
          ...eventLines.map((d) => ({ ...d, lineStyle: { color: muted, type: "dotted", width: 1 }, label: { show: eventLines.length < 8, formatter: "{b}", color: muted, fontSize: 10, position: "insideEndTop" } })),
        ],
      },
    });
    if (setpoint) {
      series.push({
        name: setpoint.channel.name,
        type: "line",
        step: "end",
        data: stepPoints(setpoint),
        showSymbol: false,
        connectNulls: false,
        lineStyle: { width: 1.5, type: "dashed", color: spColor },
        itemStyle: { color: spColor },
        xAxisIndex: 0,
        yAxisIndex: 0,
      });
    }
    if (state) {
      series.push({
        name: state.channel.name,
        type: "line",
        step: "end",
        data: stepPoints(state),
        showSymbol: false,
        connectNulls: false,
        lineStyle: { width: 2, color: stColor },
        itemStyle: { color: stColor },
        areaStyle: { opacity: 0.08, color: stColor },
        xAxisIndex: 1,
        yAxisIndex: 1,
      });
    }

    const timeLabel = (v: number) => {
      const span = end - start;
      return formatTime(v, tz, span > 36 * 3600_000, false).replace(/:\d\d$/, span < 3600_000 ? "$&" : "");
    };

    c.setOption(
      {
        animation: false,
        aria: { enabled: true, decal: { show: true }, label: { description: `${title}. Time axis in ${tzLabel}.` } },
        title: { text: title, left: 8, top: 0, textStyle: { fontSize: 13, color: fg, fontWeight: 600 } },
        toolbox: { right: 8, top: 0, feature: { saveAsImage: { title: "Save PNG", name: title.replace(/[^A-Za-z0-9_-]+/g, "_"), pixelRatio: 2 } } },
        legend: { top: 22, left: 8, textStyle: { color: fg }, data: series.map((s) => (s as { name: string }).name).filter((n) => n !== "min") },
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "line" },
          formatter: (params: unknown) => {
            const list = params as { seriesName: string; value: Pt; axisValue: number }[];
            if (!list.length) return "";
            const t = list[0]!.axisValue;
            const lines = [`<b>${formatTime(t, tz, true, true)} ${tzLabel}</b>`];
            for (const p of list) {
              if (p.seriesName === "min") continue;
              const v = p.value?.[1];
              if (p.seriesName.endsWith("min–max") && measured.mode === "bucket") {
                const b = measured.buckets?.find((x) => Math.abs(Date.parse(x.t) + (measured.bucket_s ?? 0) * 500 - p.value[0]) < 1);
                if (b && b.min !== null && b.max !== null) {
                  lines.push(`max ${b.max.toFixed(prec)} ${unit} at ${formatTime(b.max_t!, tz, false, true)}`);
                  lines.push(`min ${b.min.toFixed(prec)} ${unit} at ${formatTime(b.min_t!, tz, false, true)}`);
                  lines.push(`${b.valid_count}/${b.count} valid in ${measured.bucket_s} s bucket`);
                }
                continue;
              }
              if (p.seriesName === state?.channel.name) {
                lines.push(`${p.seriesName}: ${v === null || v === undefined ? "—" : labels[String(v)] ?? v}`);
              } else {
                lines.push(`${p.seriesName}: ${v === null || v === undefined ? "—" : `${Number(v).toFixed(prec)} ${unit}`}`);
              }
            }
            return lines.join("<br/>");
          },
        },
        grid: [
          { left: 76, right: 24, top: 56, bottom: state ? 150 : 60 },
          ...(state ? [{ left: 76, right: 24, height: 60, bottom: 50 }] : []),
        ],
        xAxis: [
          { type: "time", min: start, max: end, gridIndex: 0, axisLabel: { show: !state, color: muted, formatter: timeLabel, hideOverlap: true }, splitLine: { show: true, lineStyle: { color: gridLine } } },
          ...(state
            ? [{ type: "time", min: start, max: end, gridIndex: 1, name: `Time (${tzLabel})`, nameLocation: "middle", nameGap: 28, nameTextStyle: { color: muted }, axisLabel: { color: muted, formatter: timeLabel, hideOverlap: true }, splitLine: { show: true, lineStyle: { color: gridLine } } }]
            : []),
        ],
        yAxis: [
          {
            type: "value",
            gridIndex: 0,
            name: `${measured.channel.name} (${unit})`,
            nameLocation: "middle",
            nameGap: 48,
            nameTextStyle: { color: muted },
            scale: true,
            min: yScale.mode === "fixed" ? yScale.min : autoMin,
            max: yScale.mode === "fixed" ? yScale.max : autoMax,
            axisLabel: { color: muted },
            splitLine: { lineStyle: { color: gridLine } },
          },
          ...(state
            ? [{
                type: "value",
                gridIndex: 1,
                min: Math.min(...codes, 0),
                max: Math.max(...codes, 0),
                interval: 1,
                name: state.channel.name,
                nameLocation: "middle",
                nameGap: 52,
                nameTextStyle: { color: muted, fontSize: 10 },
                axisLabel: { color: muted, fontSize: 10, formatter: (v: number) => labels[String(v)] ?? "" },
                splitLine: { show: false },
              }]
            : []),
        ],
        dataZoom: [{ type: "inside", xAxisIndex: state ? [0, 1] : [0], filterMode: "none", start: 0, end: 100, zoomOnMouseWheel: "shift", moveOnMouseMove: false }],
        series,
      },
      { notMerge: true },
    );
  }, [measured, setpoint, state, start, end, tz, tzLabel, yScale, title]);

  return <div ref={el} className="chart" role="img" aria-label={`${title} chart`} data-testid="history-chart" />;
}
