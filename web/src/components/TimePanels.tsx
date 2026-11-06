import { useEffect, useRef } from "react";
import { echarts } from "../charts";
import { axisLabel, formatNumber, humanize } from "../format";
import { formatDuration, formatTime } from "../time";
import type { ChannelStatus, PanelDef, Series } from "../types";

export interface PanelScale {
  mode: "auto" | "log" | "fixed";
  min: number;
  max: number;
}

type Pt = [number, number | null];

const MIN_SPAN: Record<string, number> = { temperature: 2, "length change": 1 };
const SHOWN_EVENTS = new Set([
  "run_started", "run_ended", "source_unavailable", "source_recovered", "clock_jump",
  "schedule_skipped", "acquisition_lost_interval", "spool_epoch_started",
]);

export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

function midGaps(s: Series): Pt[] {
  return s.gaps.map((g) => [(Date.parse(g.start) + Date.parse(g.end)) / 2, null]);
}

function linePts(s: Series): Pt[] {
  const half = (s.bucket_s ?? 0) * 500;
  const pts: Pt[] =
    s.mode === "raw"
      ? (s.points ?? []).map((p) => [Date.parse(p[0]), p[1]])
      : (s.buckets ?? []).map((b) => [Date.parse(b.t) + half, b.mean]);
  return [...pts, ...midGaps(s)].sort((a, b) => a[0] - b[0]);
}

function stepPts(s: Series): Pt[] {
  if (s.mode === "raw") return linePts(s);
  const pts: Pt[] = (s.buckets ?? []).map((b) => [Date.parse(b.t), b.last]);
  return [...pts, ...midGaps(s)].sort((a, b) => a[0] - b[0]);
}

function bandPts(s: Series): { lo: Pt[]; span: Pt[]; hi: Pt[] } {
  const half = (s.bucket_s ?? 0) * 500;
  const lo: Pt[] = [];
  const span: Pt[] = [];
  const hi: Pt[] = [];
  for (const b of s.buckets ?? []) {
    const t = Date.parse(b.t) + half;
    lo.push([t, b.min]);
    hi.push([t, b.max]);
    span.push([t, b.min === null || b.max === null ? null : b.max - b.min]);
  }
  for (const g of midGaps(s)) {
    lo.push(g);
    hi.push(g);
    span.push(g);
  }
  const by = (a: Pt, b: Pt) => a[0] - b[0];
  return { lo: lo.sort(by), span: span.sort(by), hi: hi.sort(by) };
}

export function TimePanels(props: {
  title: string;
  panels: PanelDef[];
  channels: ChannelStatus[];
  series: Map<string, Series>;
  start: number;
  end: number;
  tz: string;
  tzLabel: string;
  scales: Record<string, PanelScale>;
  tolerance: number | null;
  theme: string;
  onZoom?: (start: number, end: number) => void;
  height?: number;
}) {
  const { title, panels, channels, series, start, end, tz, tzLabel, scales, tolerance, theme, onZoom } = props;
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const zoomRef = useRef(onZoom);
  zoomRef.current = onZoom;
  const rangeRef = useRef({ start, end });
  rangeRef.current = { start, end };

  const isState = (p: PanelDef) =>
    p.channels.every((k) => {
      const c = channels.find((x) => x.key === k);
      return c?.data_type === "enum" || c?.data_type === "bool";
    });
  const numericCount = panels.filter((p) => !isState(p)).length;
  const stateCount = panels.length - numericCount;
  const numericH = numericCount <= 1 ? 300 : numericCount === 2 ? 210 : 170;
  const stateH = 56;
  const gapH = 30;
  const top = 64;
  const bottom = 52;
  const height = props.height ?? top + bottom + numericCount * numericH + stateCount * stateH + (panels.length - 1) * gapH;

  useEffect(() => {
    if (!el.current) return;
    const c = echarts.init(el.current, undefined, { renderer: "canvas" });
    chart.current = c;
    let timer: number | undefined;
    c.on("datazoom", () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        const opt = c.getOption() as { dataZoom?: { start?: number; end?: number }[] };
        const dz = opt.dataZoom?.[0];
        if (!dz || dz.start === undefined || dz.end === undefined || !zoomRef.current) return;
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
    if (!c) return;
    c.resize();
    const fg = cssVar("--fg");
    const muted = cssVar("--muted");
    const gridLine = cssVar("--grid");
    const gapFill = cssVar("--gap");
    const bad = cssVar("--bad");
    const palette = ["--series-1", "--series-2", "--series-3", "--series-4"].map(cssVar);
    const chByKey = new Map(channels.map((ch) => [ch.key, ch]));
    const grids: object[] = [];
    const xAxes: object[] = [];
    const yAxes: object[] = [];
    const out: object[] = [];
    const legend: string[] = [];
    const span = end - start;
    const timeLabel = (v: number) => formatTime(v, tz, span > 36 * 3600_000, false);

    let y = top;
    panels.forEach((p, i) => {
      const state = isState(p);
      const h = state ? stateH : numericH;
      const last = i === panels.length - 1;
      grids.push({ left: 84, right: 28, top: y, height: h });
      xAxes.push({
        type: "time", gridIndex: i, min: start, max: end,
        axisLabel: { show: last, color: muted, formatter: timeLabel, hideOverlap: true },
        axisTick: { show: last }, splitLine: { show: true, lineStyle: { color: gridLine } },
        ...(last ? { name: `Time (${tzLabel})`, nameLocation: "middle", nameGap: 30, nameTextStyle: { color: muted } } : {}),
      });
      y += h + gapH;
      const chs = p.channels.map((k) => chByKey.get(k)).filter((x): x is ChannelStatus => !!x);
      const primary = chs.find((ch) => ch.role !== "setpoint") ?? chs[0];
      if (!primary) return;
      if (state) {
        const labels = primary.enum_labels ?? {};
        const codes = Object.keys(labels).map(Number);
        yAxes.push({
          type: "value", gridIndex: i, min: Math.min(0, ...codes), max: Math.max(0, ...codes), interval: 1,
          name: p.title, nameLocation: "middle", nameGap: 62, nameTextStyle: { color: muted, fontSize: 11 },
          axisLabel: { color: muted, fontSize: 10, formatter: (v: number) => labels[String(v)] ?? "" },
          splitLine: { show: false },
        });
        const s = series.get(primary.key);
        if (s) {
          out.push({
            name: primary.name, type: "line", step: "end", data: stepPts(s), showSymbol: false, connectNulls: false,
            xAxisIndex: i, yAxisIndex: i, lineStyle: { width: 2, color: palette[2] }, itemStyle: { color: palette[2] },
            areaStyle: { opacity: 0.1, color: palette[2] },
          });
          legend.push(primary.name);
        }
        return;
      }
      const sc = scales[p.key] ?? { mode: p.scale === "log" ? "log" : "auto", min: 0, max: 1 };
      const log = sc.mode === "log";
      const sci = primary.display_format === "scientific";
      const minSpan = MIN_SPAN[primary.quantity] ?? 0;
      const pad = (v: { min: number; max: number }, dir: 1 | -1) => {
        const width = Math.max(v.max - v.min, minSpan, 1e-9);
        const mid = (v.max + v.min) / 2;
        const raw = mid + dir * (width / 2 + width * 0.06);
        // snap the axis end outward to a round step so its label reads cleanly
        const step = 10 ** Math.floor(Math.log10(width / 5));
        return (dir > 0 ? Math.ceil(raw / step) : Math.floor(raw / step)) * step;
      };
      yAxes.push({
        type: log ? "log" : "value", gridIndex: i, logBase: 10, scale: true,
        name: `${p.title}${primary.unit ? ` (${primary.unit})` : ""}`, nameLocation: "middle", nameGap: 62,
        nameTextStyle: { color: muted, fontSize: 11 },
        ...(sc.mode === "fixed" ? { min: sc.min, max: sc.max } : log ? {} : { min: (v: { min: number; max: number }) => pad(v, -1), max: (v: { min: number; max: number }) => pad(v, 1) }),
        axisLabel: { color: muted, formatter: (v: number) => axisLabel(v, sci || log) },
        splitLine: { lineStyle: { color: gridLine } },
      });
      chs.forEach((ch, j) => {
        const s = series.get(ch.key);
        if (!s) return;
        const color = ch.role === "setpoint" ? palette[1]! : palette[j === 0 ? 0 : (j + 2) % palette.length]!;
        if (ch.role === "setpoint") {
          out.push({
            name: ch.name, type: "line", step: "end", data: stepPts(s), showSymbol: false, connectNulls: false,
            xAxisIndex: i, yAxisIndex: i, lineStyle: { width: 1.5, type: "dashed", color }, itemStyle: { color },
          });
          legend.push(ch.name);
          if (p.tolerance_of === ch.key && tolerance) {
            const band = (sign: number): Pt[] => stepPts(s).map(([t, v]) => [t, v === null ? null : v + sign * tolerance]);
            const name = `Tolerance ±${tolerance} ${ch.unit}`;
            for (const sign of [1, -1]) {
              out.push({
                name, type: "line", step: "end", data: band(sign), showSymbol: false, connectNulls: false, silent: true,
                xAxisIndex: i, yAxisIndex: i, lineStyle: { width: 1, type: "dotted", color }, itemStyle: { color }, tooltip: { show: false },
              });
            }
            legend.push(name);
          }
          return;
        }
        if (s.mode === "bucket") {
          const b = bandPts(s);
          if (log) {
            for (const [nm, data] of [["max", b.hi], ["min", b.lo]] as const) {
              out.push({ name: `${ch.name} ${nm}`, type: "line", data, showSymbol: false, connectNulls: false, silent: true, xAxisIndex: i, yAxisIndex: i, lineStyle: { width: 0.8, opacity: 0.6, color }, tooltip: { show: false } });
            }
          } else {
            const stack = `band-${p.key}-${ch.key}`;
            out.push({ name: `${ch.name} min`, type: "line", data: b.lo, stack, showSymbol: false, connectNulls: false, silent: true, xAxisIndex: i, yAxisIndex: i, lineStyle: { opacity: 0 }, tooltip: { show: false } });
            out.push({ name: `${ch.name} range`, type: "line", data: b.span, stack, showSymbol: false, connectNulls: false, silent: true, xAxisIndex: i, yAxisIndex: i, lineStyle: { opacity: 0 }, areaStyle: { color, opacity: 0.22 }, tooltip: { show: false } });
          }
        }
        const bads =
          s.mode === "raw"
            ? (s.points ?? []).filter((pt) => pt[2] === "bad").slice(0, 150).map((pt) => ({ xAxis: Date.parse(pt[0]), name: `bad: ${humanize(pt[3] ?? "")}` }))
            : (s.buckets ?? []).filter((bk) => bk.bad_count > 0).slice(0, 150).map((bk) => ({ xAxis: Date.parse(bk.t), name: `bad ×${bk.bad_count}` }));
        const events = i === 0
          ? s.events.filter((e) => SHOWN_EVENTS.has(e.type)).slice(0, 60).map((e) => ({
              xAxis: Date.parse(e.at),
              name: e.type === "run_started" ? `▶ ${String(e.detail?.name ?? "run")}` : e.type === "run_ended" ? "■ run end" : humanize(e.type),
            }))
          : [];
        const name = s.mode === "bucket" ? `${ch.name} (mean)` : ch.name;
        legend.push(name);
        out.push({
          name, type: "line", data: linePts(s), connectNulls: false, smooth: false,
          showSymbol: s.mode === "raw" && (s.points?.length ?? 0) < 300, symbolSize: 4,
          xAxisIndex: i, yAxisIndex: i, lineStyle: { width: 1.6, color }, itemStyle: { color },
          markArea: j === 0 ? {
            silent: true, itemStyle: { color: gapFill },
            label: { color: muted, fontSize: 10, position: "insideTop" },
            data: s.gaps.map((g) => [{ xAxis: Date.parse(g.start), name: g.open ? "no data yet" : `no data · ${formatDuration(g.duration_s)}` }, { xAxis: Date.parse(g.end) }]),
          } : undefined,
          markLine: j === 0 ? {
            silent: true, symbol: "none",
            data: [
              ...bads.map((d) => ({ ...d, lineStyle: { color: bad, type: "dashed", width: 1 }, label: { show: bads.length < 10, formatter: "{b}", color: bad, fontSize: 10 } })),
              ...events.map((d) => ({ ...d, lineStyle: { color: muted, type: "dotted", width: 1 }, label: { show: events.length <= 4, formatter: "{b}", color: muted, fontSize: 10, position: "insideEndTop" } })),
            ],
          } : undefined,
        });
      });
    });

    const fmtFor = (seriesName: string) => channels.find((ch) => seriesName.startsWith(ch.name));
    c.setOption(
      {
        animation: false,
        aria: { enabled: true, label: { description: `${title}. Time axis in ${tzLabel}.` } },
        title: { text: title, left: 8, top: 2, textStyle: { fontSize: 13, color: fg, fontWeight: 600 } },
        toolbox: { right: 12, top: 0, iconStyle: { borderColor: muted }, feature: { saveAsImage: { title: "Save PNG", name: title.replace(/[^A-Za-z0-9_-]+/g, "_"), pixelRatio: 2, backgroundColor: cssVar("--surface") } } },
        legend: { type: "scroll", top: 26, left: 8, right: 40, textStyle: { color: fg }, data: [...new Set(legend)] },
        axisPointer: { link: [{ xAxisIndex: "all" }] },
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "line" },
          formatter: (params: unknown) => {
            const list = params as { seriesName: string; value: Pt; axisValue: number }[];
            if (!list.length) return "";
            const lines = [`<b>${formatTime(list[0]!.axisValue, tz, true, true)} ${tzLabel}</b>`];
            for (const p of list) {
              const v = p.value?.[1];
              const ch = fmtFor(p.seriesName);
              if (!ch) continue;
              const shown = v === null || v === undefined ? "—" : ch.data_type === "enum" ? ch.enum_labels?.[String(v)] ?? String(v) : `${formatNumber(ch, v)} ${ch.unit}`;
              lines.push(`${p.seriesName}: ${shown}`);
            }
            return lines.join("<br/>");
          },
        },
        grid: grids,
        xAxis: xAxes,
        yAxis: yAxes,
        dataZoom: [{ type: "inside", xAxisIndex: panels.map((_, i) => i), filterMode: "none", zoomOnMouseWheel: "shift", moveOnMouseMove: false }],
        series: out,
      },
      { notMerge: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [series, panels, channels, start, end, tz, tzLabel, scales, tolerance, theme, title, height]);

  return <div ref={el} className="chart" style={{ height }} role="img" aria-label={`${title} chart`} data-testid="history-chart" />;
}
