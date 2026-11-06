import { useMemo, useState } from "react";
import { api, ApiError, qs } from "../api";
import { formatNumber, formatValue, humanize } from "../format";
import { useNow, usePolling } from "../hooks";
import { acquisitionReason } from "../status";
import { formatAge, formatDuration, formatTime, fromLocalInput, isoUtc, toLocalInput } from "../time";
import type { AcqEvent, AnnotationView, ChannelStatus, InstrumentStatus, PanelDef, RunDetail, RunView, Series, SoakTolerance } from "../types";
import { AcqBadge, CollectorBadge, RunBadge, SimBadge } from "./Badges";
import { ConnectionBanner } from "./ConnectionBanner";
import { type PanelScale, TimePanels } from "./TimePanels";
import { AnnotationPanel, NewRunForm, RECORD_ROLES, RunEditForm } from "./Records";
import { ExportsPanel } from "./Exports";
import { XYChart } from "./XYChart";

const PRESETS = [
  { key: "15m", label: "15 min", ms: 15 * 60_000 },
  { key: "1h", label: "1 h", ms: 3600_000 },
  { key: "24h", label: "24 h", ms: 24 * 3600_000 },
  { key: "7d", label: "7 d", ms: 7 * 86400_000 },
  { key: "30d", label: "30 d", ms: 30 * 86400_000 },
] as const;

type View = { kind: "live"; preset: (typeof PRESETS)[number]["key"] } | { kind: "fixed"; start: number; end: number; reason: string };
type Tab = "overview" | "runs" | "diagnostics";
const NO_SCALES: Record<string, PanelScale> = {};

interface Common {
  canRecord?: boolean;
  tz: string;
  tzLabel: string;
  theme: string;
}

/** Chart inputs that change only with the channel set or profile revision, not every poll. */
function useStableMeta(s: InstrumentStatus): { panels: PanelDef[]; channels: ChannelStatus[] } {
  const sig = `${s.display.profile_revision_id}|${s.channels.map((c) => c.id).join(",")}`;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  return useMemo(() => ({ panels: panelsOf(s), channels: s.channels }), [sig]);
}

function panelsOf(s: InstrumentStatus): PanelDef[] {
  if (s.display.panels.length) return s.display.panels;
  return s.channels.map((c) => ({ key: c.key, title: c.name, channels: [c.key], scale: "linear", log_allowed: false, tolerance_of: null }));
}

function spanOf(view: View): number {
  return view.kind === "fixed" ? view.end - view.start : PRESETS.find((p) => p.key === view.preset)!.ms;
}

function liveInterval(ms: number): number {
  return ms <= 3600_000 ? 2000 : ms <= 86400_000 ? 10000 : 30000;
}

/** Poll every plotted channel of an instrument over a live or fixed window. */
function useSeries(channels: ChannelStatus[], key: string, window: () => { start: number; end: number }, intervalMs: number) {
  return usePolling<{ start: number; end: number; series: Map<string, Series> }>(
    channels.length
      ? async (signal) => {
          const { start, end } = window();
          const list = await Promise.all(
            channels.map((c) =>
              api.get<Series>(`/api/v1/channels/${encodeURIComponent(c.id)}/series?${qs({ start: isoUtc(start), end: isoUtc(end), max_points: 5000 })}`, signal),
            ),
          );
          return { start, end, series: new Map(list.map((s, i) => [channels[i]!.key, s])) };
        }
      : null,
    intervalMs,
    key,
  );
}

function Tile({ ch, tz, drift }: { ch: ChannelStatus; tz: string; drift: number }) {
  const latest = ch.latest;
  const good = ch.last_good;
  const showGood = good && latest && good.event_time !== latest.event_time;
  return (
    <div className={`card tile ${ch.fresh ? "" : "stale"}`} data-testid={`tile-${ch.key}`}>
      <div className="tile-name">
        {ch.name}
        {ch.note ? <span className="faint"> · {ch.note}</span> : null}
      </div>
      <div className={`tile-value ${latest?.quality === "bad" ? "q-bad" : ""}`}>{formatValue(ch, latest)}</div>
      <div className="sub">
        {latest ? (
          <>
            {formatTime(latest.event_time, tz, false)} · {formatAge(latest.age_s + drift)}
            {!ch.fresh && <strong> · not fresh</strong>}
            {latest.quality === "uncertain" && <strong> · uncertain</strong>}
          </>
        ) : (
          "no samples"
        )}
      </div>
      {showGood && <div className="sub">last good {formatValue(ch, good)} at {formatTime(good.event_time, tz, false)}</div>}
    </div>
  );
}

function ScaleControls({ panels, channels, scales, setScales }: { panels: PanelDef[]; channels: ChannelStatus[]; scales: Record<string, PanelScale>; setScales: (s: Record<string, PanelScale>) => void }) {
  const numeric = panels.filter((p) => p.channels.some((k) => channels.find((c) => c.key === k)?.data_type === "float"));
  return (
    <div className="axis-controls" aria-label="Axis scaling">
      {numeric.map((p) => {
        const sc = scales[p.key] ?? { mode: p.scale === "log" ? "log" : "auto", min: 0, max: 1 };
        const set = (patch: Partial<PanelScale>) => setScales({ ...scales, [p.key]: { ...sc, ...patch } });
        return (
          <fieldset key={p.key}>
            <legend>{p.title}</legend>
            <select aria-label={`${p.title} axis`} value={sc.mode} onChange={(e) => set({ mode: e.target.value as PanelScale["mode"] })}>
              <option value="auto">Auto</option>
              {(p.log_allowed || p.scale === "log") && <option value="log">Log</option>}
              <option value="fixed">Fixed</option>
            </select>
            {sc.mode === "fixed" && (
              <>
                <input className="narrow" type="number" aria-label={`${p.title} minimum`} value={sc.min} onChange={(e) => set({ min: Number(e.target.value) })} />
                <input className="narrow" type="number" aria-label={`${p.title} maximum`} value={sc.max} onChange={(e) => set({ max: Number(e.target.value) })} />
              </>
            )}
          </fieldset>
        );
      })}
    </div>
  );
}

function SeriesSummary({ s, ch, tz, tzLabel }: { s: Series; ch: ChannelStatus; tz: string; tzLabel: string }) {
  let n = 0;
  let valid = 0;
  let min: [number, string] | null = null;
  let max: [number, string] | null = null;
  if (s.mode === "raw") {
    for (const p of s.points ?? []) {
      n++;
      if (p[1] === null) continue;
      valid++;
      if (!min || p[1] < min[0]) min = [p[1], p[0]];
      if (!max || p[1] > max[0]) max = [p[1], p[0]];
    }
  } else {
    for (const b of s.buckets ?? []) {
      n += b.count;
      valid += b.valid_count;
      if (b.min !== null && (!min || b.min < min[0])) min = [b.min, b.min_t!];
      if (b.max !== null && (!max || b.max > max[0])) max = [b.max, b.max_t!];
    }
  }
  const gapTotal = s.gaps.reduce((a, g) => a + g.duration_s, 0);
  let sum = 0;
  if (s.mode === "raw") for (const p of s.points ?? []) sum += p[1] ?? 0;
  else for (const b of s.buckets ?? []) sum += b.mean !== null ? b.mean * b.valid_count : 0;
  const mean = valid > 0 && ch.data_type === "float" ? sum / valid : null;
  const span = (Math.min(Date.parse(s.range.end), Date.parse(s.server_time)) - Date.parse(s.range.start)) / 1000;
  const expected = span > 0 ? span / ch.expected_cadence_s : 0;
  const coverage = expected > 0 ? Math.min(1, valid / expected) : null;
  return (
    <p className="summary" data-testid="series-summary">
      <strong>{ch.name}:</strong> {n.toLocaleString()} samples ({valid.toLocaleString()} valid{n - valid > 0 ? `, ${(n - valid).toLocaleString()} bad` : ""})
      {max && ` · max ${formatNumber(ch, max[0])} ${ch.unit} at ${formatTime(max[1], tz, true)} ${tzLabel}`}
      {min && ` · min ${formatNumber(ch, min[0])} ${ch.unit} at ${formatTime(min[1], tz, true)}`}
      {mean !== null && ` · mean ${formatNumber(ch, mean)} ${ch.unit} (valid samples)`}
      {coverage !== null && ` · coverage ${(coverage * 100).toFixed(1)} % of expected ${ch.expected_cadence_s} s cadence`} ·{" "}
      {s.gaps.length === 0 ? "no gaps" : `${s.gaps.length} gap${s.gaps.length > 1 ? "s" : ""} (${formatDuration(gapTotal)} without data)`} ·{" "}
      {s.mode === "raw" ? "raw samples" : `aggregated in ${formatDuration(s.bucket_s ?? 0)} buckets (band = min–max, line = mean)`}
      {s.rebuilding_minutes ? ` · ${s.rebuilding_minutes} recent minute${s.rebuilding_minutes > 1 ? "s" : ""} aggregated from raw while rollups catch up` : ""}
    </p>
  );
}

function ReadingsTable({ s, ch, tz, tzLabel }: { s: Series; ch: ChannelStatus; tz: string; tzLabel: string }) {
  return (
    <details className="readings">
      <summary>Readings table · {ch.name} ({s.mode === "raw" ? "latest 100 raw samples" : "latest 100 buckets"})</summary>
      <div className="table-wrap">
        {s.mode === "raw" ? (
          <table>
            <thead>
              <tr>
                <th scope="col">Time ({tzLabel})</th>
                <th scope="col">Value ({ch.unit || "code"})</th>
                <th scope="col">Quality</th>
                <th scope="col">Clock</th>
                <th scope="col">Identity</th>
              </tr>
            </thead>
            <tbody>
              {(s.points ?? []).slice(-100).reverse().map((p) => (
                <tr key={p[5]}>
                  <td className="mono">{formatTime(p[0], tz, true, true)}</td>
                  <td className="num">{p[1] === null ? "—" : formatNumber(ch, p[1])}</td>
                  <td>{p[2]}{p[3] ? ` (${humanize(p[3])})` : ""}</td>
                  <td>{p[4]}</td>
                  <td className="sub mono">{p[5]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <table>
            <thead>
              <tr>
                <th scope="col">Bucket start ({tzLabel})</th>
                <th scope="col">Min</th>
                <th scope="col">Mean</th>
                <th scope="col">Max</th>
                <th scope="col">Valid / count</th>
              </tr>
            </thead>
            <tbody>
              {(s.buckets ?? []).slice(-100).reverse().map((b) => (
                <tr key={b.t}>
                  <td className="mono">{formatTime(b.t, tz, true)}</td>
                  <td className="num">{b.min === null ? "—" : formatNumber(ch, b.min)}</td>
                  <td className="num">{b.mean === null ? "—" : formatNumber(ch, b.mean)}</td>
                  <td className="num">{b.max === null ? "—" : formatNumber(ch, b.max)}</td>
                  <td>{b.valid_count} / {b.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </details>
  );
}

// --- Overview -------------------------------------------------------------------------------

function Overview({ s, drift, tz, tzLabel, theme, canRecord }: Common & { s: InstrumentStatus; drift: number }) {
  const [view, setView] = useState<View>({ kind: "live", preset: "1h" });
  const [custom, setCustom] = useState<{ start: string; end: string } | null>(null);
  const [rangeError, setRangeError] = useState<string | null>(null);
  const [scales, setScales] = useState<Record<string, PanelScale>>({});
  const { panels, channels: meta } = useStableMeta(s);
  const plotted = meta.filter((c) => panels.some((p) => p.channels.includes(c.key)));
  const span = spanOf(view);
  const viewKey = view.kind === "live" ? `live:${view.preset}` : `fixed:${view.start}:${view.end}`;
  const history = useSeries(
    plotted,
    `${s.instrument.id}|${viewKey}|${plotted.map((c) => c.id).join(",")}`,
    () => {
      const end = view.kind === "live" ? Date.now() + 1000 : view.end;
      return { start: view.kind === "live" ? end - span : view.start, end };
    },
    view.kind === "live" ? liveInterval(span) : 15000,
  );
  const range = history.data ? { start: history.data.start, end: history.data.end } : null;
  const notes = usePolling<{ items: AnnotationView[] }>(
    (signal) => {
      const end = view.kind === "live" ? Date.now() + 1000 : view.end;
      const start = view.kind === "live" ? end - span : view.start;
      return api.get(`/api/v1/instruments/${encodeURIComponent(s.instrument.id)}/annotations?${qs({ start: isoUtc(start), end: isoUtc(end) })}`, signal);
    },
    view.kind === "live" ? Math.max(liveInterval(span), 5000) : 30000,
    `ann:${s.instrument.id}|${viewKey}`,
  );
  const annotations = notes.data?.items;
  const pause = (start: number, end: number, reason: string) => {
    setView({ kind: "fixed", start, end, reason });
    setCustom(null);
  };
  const zoom = (factor: number) => {
    if (!range) return;
    const mid = (range.start + range.end) / 2;
    const half = Math.max(30_000, ((range.end - range.start) * factor) / 2);
    pause(Math.round(mid - half), Math.round(Math.min(mid + half, Date.now() + 1000)), "zoomed");
  };
  const pan = (dir: number) => {
    if (!range) return;
    const w = range.end - range.start;
    pause(range.start + dir * w * 0.5, range.end + dir * w * 0.5, "panned");
  };
  const applyCustom = () => {
    if (!custom) return;
    const a = fromLocalInput(custom.start, tz);
    const b = fromLocalInput(custom.end, tz);
    if (!Number.isFinite(a) || !Number.isFinite(b) || b <= a) return setRangeError("End must be after start.");
    if (b - a > 31 * 86400_000) return setRangeError("Ranges are limited to 31 days.");
    setRangeError(null);
    pause(a, b, "custom range");
  };
  const csvHref = range ? `/api/v1/instruments/${encodeURIComponent(s.instrument.id)}/export.csv?${qs({ start: isoUtc(range.start), end: isoUtc(range.end) })}` : undefined;
  const primary = plotted.find((c) => c.role === "measurement") ?? plotted[0];
  const primarySeries = primary ? history.data?.series.get(primary.key) : undefined;
  const title = `${s.instrument.name}${s.instrument.simulated ? " [SIMULATED]" : ""}${range ? ` · ${formatTime(range.start, tz)} – ${formatTime(range.end, tz)} ${tzLabel}` : ""}`;

  return (
    <>
      <section className="tiles" aria-label="Latest readings">
        {s.channels.map((c) => (
          <Tile key={c.id} ch={c} tz={tz} drift={drift} />
        ))}
      </section>
      <section className="card card-pad" aria-labelledby="history-title">
        <div className="toolbar">
          <h2 id="history-title">History</h2>
          <div className="segmented" role="group" aria-label="Time range">
            {PRESETS.map((p) => (
              <button key={p.key} type="button" aria-pressed={view.kind === "live" && view.preset === p.key} onClick={() => setView({ kind: "live", preset: p.key })}>
                {p.label}
              </button>
            ))}
            <button
              type="button"
              aria-pressed={custom !== null}
              onClick={() => setCustom(custom ?? { start: toLocalInput(range?.start ?? Date.now() - 3600_000, tz), end: toLocalInput(range?.end ?? Date.now(), tz) })}
            >
              Custom…
            </button>
          </div>
          <span className="spacer" />
          <span data-testid="live-state" className="row">
            {view.kind === "live" ? (
              <span className="pill tone-ok">
                <span className="dot" aria-hidden="true" /> Live — following latest
              </span>
            ) : (
              <>
                <span className="pill tone-muted">‖ Paused ({view.reason})</span>
                <button type="button" className="btn-primary" onClick={() => setView({ kind: "live", preset: "1h" })}>
                  Resume live
                </button>
              </>
            )}
          </span>
        </div>
        {custom && (
          <div className="toolbar">
            <label>
              Start ({tzLabel}) <input type="datetime-local" value={custom.start} onChange={(e) => setCustom({ ...custom, start: e.target.value })} />
            </label>
            <label>
              End ({tzLabel}) <input type="datetime-local" value={custom.end} onChange={(e) => setCustom({ ...custom, end: e.target.value })} />
            </label>
            <button type="button" onClick={applyCustom}>
              Apply range
            </button>
            {rangeError && (
              <span className="error" role="alert">
                {rangeError}
              </span>
            )}
          </div>
        )}
        <div className="toolbar">
          <span className="segmented" role="group" aria-label="Zoom and pan">
            <button type="button" onClick={() => pan(-1)} aria-label="Pan earlier">◀</button>
            <button type="button" onClick={() => zoom(0.5)}>Zoom in</button>
            <button type="button" onClick={() => zoom(2)}>Zoom out</button>
            <button type="button" onClick={() => pan(1)} aria-label="Pan later">▶</button>
          </span>
          <ScaleControls panels={panels} channels={meta} scales={scales} setScales={setScales} />
          <span className="spacer" />
          {csvHref && (
            <a className="btn" href={csvHref} download data-testid="csv-link">
              ⤓ CSV (raw rows)
            </a>
          )}
        </div>
        <p className="sub">Shift + mouse wheel on the chart zooms (the view then pauses). The PNG button saves the chart with its labels.</p>
        {history.error && (
          <p className="error" role="alert">
            History request failed: {history.error.message}. Retrying…
          </p>
        )}
        {history.loading && !history.data && <p className="muted">Loading history…</p>}
        {range && history.data && (
          <>
            <TimePanels
              title={title}
              panels={panels}
              channels={meta}
              series={history.data.series}
              start={range.start}
              end={range.end}
              tz={tz}
              tzLabel={tzLabel}
              scales={scales}
              tolerance={s.display.tolerance}
              theme={theme}
              onZoom={(a, b) => pause(a, b, "zoomed")}
              annotations={annotations}
            />
            {primary && primarySeries && (
              <>
                <SeriesSummary s={primarySeries} ch={primary} tz={tz} tzLabel={tzLabel} />
                <ReadingsTable s={primarySeries} ch={primary} tz={tz} tzLabel={tzLabel} />
              </>
            )}
          </>
        )}
      </section>
      <div style={{ height: 14 }} />
      <AnnotationPanel instrumentId={s.instrument.id} items={annotations ?? []} canRecord={!!canRecord} tz={tz} tzLabel={tzLabel} onChanged={notes.refresh} />
      <div style={{ height: 14 }} />
      <ExportsPanel instrumentId={s.instrument.id} range={range} tz={tz} tzLabel={tzLabel} />
    </>
  );
}

// --- Runs -----------------------------------------------------------------------------------

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="card metric">
      <div className="m-label">{label}</div>
      <div className="m-value">{value}</div>
      {hint && <div className="sub">{hint}</div>}
    </div>
  );
}

function num(v: unknown, digits: number, unit = ""): string {
  return typeof v === "number" ? `${v.toFixed(digits)}${unit ? ` ${unit}` : ""}` : "—";
}

function RunAnalysisView({ detail, s, tz, tzLabel, theme, canRecord, onChanged }: Common & { detail: RunDetail; s: InstrumentStatus; onChanged: () => void }) {
  const a = detail.analysis;
  const run = detail.run;
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const endNow = async () => {
    setError(null);
    try {
      await api.patch(`/api/v1/runs/${encodeURIComponent(run.id)}`, { ended_at: isoUtc(Date.now()), reason: "ended from the run view" });
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "request failed");
    }
  };
  const markers = useMemo(() => {
    if (!a) return [];
    const sm = a.summary as Record<string, unknown>;
    if (a.kind === "tensile" && typeof sm.uts_mpa === "number" && typeof sm.uts_strain === "number") return [{ name: "UTS", x: sm.uts_strain, y: sm.uts_mpa }];
    if (a.kind === "dsc") {
      const out = [];
      for (const [k, label] of [["endotherm", "Endo"], ["exotherm", "Exo"]] as const) {
        const pk = sm[k] as { sample_t_c?: number; peak_mw?: number } | null;
        if (pk?.sample_t_c !== undefined && pk.peak_mw !== undefined) out.push({ name: label, x: pk.sample_t_c, y: pk.peak_mw });
      }
      return out;
    }
    return [];
  }, [a]);
  const start = Date.parse(run.started_at);
  const end = run.ended_at ? Date.parse(run.ended_at) : Date.now();
  const { panels, channels: meta } = useStableMeta(s);
  const plotted = meta.filter((c) => panels.some((p) => p.channels.includes(c.key)));
  const windowSeries = useSeries(plotted, `run:${run.id}:${run.ended_at ?? "open"}`, () => ({ start: start - 30_000, end: Math.min(end + 30_000, Date.now() + 1000) }), run.active ? 5000 : 600_000);
  const sm = (a?.summary ?? {}) as Record<string, unknown>;
  const geo = (run.metadata.geometry ?? null) as Record<string, number> | null;
  return (
    <div className="card card-pad" data-testid="run-detail">
      <div className="toolbar">
        <h2>{run.name}</h2>
        <RunBadge active={run.active} />
        {run.simulated && <span className="badge sim">◇ SIMULATED</span>}
        <span className="spacer" />
        <span className="sub">
          {formatTime(run.started_at, tz)} – {run.ended_at ? formatTime(run.ended_at, tz) : "running"} {tzLabel} · {formatDuration((end - start) / 1000)}
        </span>
      </div>
      <div className="row" style={{ marginBottom: 10 }}>
        <span className="sub">
          {run.source === "user" ? `Recorded by ${run.created_by ?? "unknown"}` : "Reported by the instrument program"}
          {run.updated_at ? ` · corrected ${formatTime(run.updated_at, tz)}` : ""}
        </span>
        {run.tags.map((t) => (
          <span key={t} className="pill tone-muted">
            #{t}
          </span>
        ))}
        <span className="spacer" />
        {canRecord && run.source === "user" && run.active && (
          <button type="button" onClick={endNow}>
            ■ End run now
          </button>
        )}
        {canRecord && (
          <button type="button" onClick={() => setEditing(!editing)} aria-expanded={editing} data-testid="edit-run">
            ✎ Correct record
          </button>
        )}
      </div>
      {run.notes && <p className="notes">{run.notes}</p>}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {editing && (
        <RunEditForm
          run={run}
          tz={tz}
          tzLabel={tzLabel}
          onSaved={() => {
            setEditing(false);
            onChanged();
          }}
          onCancel={() => setEditing(false)}
        />
      )}
      {a?.kind === "tensile" && (
        <>
          <div className="metrics">
            <Metric label="UTS (engineering)" value={num(sm.uts_mpa, 1, "MPa")} hint={sm.complete ? `at strain ${num(sm.uts_strain, 4)}` : "reported for complete runs only"} />
            <Metric label="Rp0.2 (0.2 % offset yield)" value={num(sm.rp02_mpa, 1, "MPa")} hint={typeof sm.rp02_strain === "number" ? `at strain ${num(sm.rp02_strain, 4)}` : undefined} />
            <Metric
              label="Young's modulus (fit)"
              value={num(sm.modulus_gpa, 1, "GPa")}
              hint={(() => {
                const f = sm.modulus_fit as { points?: number; rmse_mpa?: number; note?: string } | null;
                return f ? (f.note ?? `${f.points} points · rmse ${f.rmse_mpa} MPa`) : undefined;
              })()}
            />
            <Metric label="Max force" value={num(sm.max_force_kn, 3, "kN")} />
            <Metric label="Fracture strain" value={num(sm.fracture_strain, 4)} hint={sm.fracture_after_uts === true ? "fracture after UTS ✓" : undefined} />
            <Metric label="Geometry" value={geo ? `A0 ${num(geo.area_mm2, 3, "mm²")}` : "missing"} hint={geo ? `L0 ${num(geo.gauge_length_mm, 2, "mm")} · Ø ${num(geo.diameter_mm, 2, "mm")}` : undefined} />
            <Metric label="Invalid pairs" value={String(sm.invalid_pairs ?? 0)} hint="breaks in the curve" />
          </div>
          {Array.isArray(sm.reasons) && sm.reasons.length > 0 && <p className="notice">Not reported: {(sm.reasons as string[]).join(" · ")}</p>}
        </>
      )}
      {a?.kind === "dsc" && (
        <>
          <div className="metrics">
            {(["endotherm", "exotherm"] as const).map((k) => {
              const pk = sm[k] as { peak_mw: number; sample_t_c: number; area_mj: number; specific_j_per_g?: number; onset_c?: number | null } | null;
              return (
                <Metric
                  key={k}
                  label={k === "endotherm" ? "Endotherm peak (onset)" : "Exotherm peak (onset)"}
                  value={pk ? `${pk.sample_t_c.toFixed(2)} °C` : "—"}
                  hint={pk ? `onset ${typeof pk.onset_c === "number" ? `${pk.onset_c.toFixed(2)} °C` : "—"} · ${pk.peak_mw.toFixed(2)} mW · area ${pk.area_mj.toFixed(0)} mJ${pk.specific_j_per_g !== undefined ? ` (${pk.specific_j_per_g.toFixed(1)} J/g)` : ""}` : undefined}
                />
              );
            })}
            <Metric label="Sign convention" value="exo ↑" hint={a.sign_convention ?? undefined} />
            <Metric label="Invalid pairs" value={String(sm.invalid_pairs ?? 0)} />
          </div>
          <p className="sub">Peak areas use a straight baseline across the peak window and are estimates for rehearsal, not certified results.</p>
        </>
      )}
      {detail.tolerance && <ToleranceView t={detail.tolerance} tz={tz} tzLabel={tzLabel} />}
      {a && <XYChart title={`${run.name} · ${a.y.label} vs ${a.x.label}${run.simulated ? " [SIMULATED]" : ""}`} analysis={a} tz={tz} tzLabel={tzLabel} theme={theme} markers={markers} />}
      <h3 style={{ margin: "14px 0 6px" }}>Time series for this run</h3>
      {windowSeries.data ? (
        <TimePanels
          title={`${run.name}${run.simulated ? " [SIMULATED]" : ""} · ${tzLabel}`}
          panels={panels}
          channels={meta}
          series={windowSeries.data.series}
          start={windowSeries.data.start}
          end={windowSeries.data.end}
          tz={tz}
          tzLabel={tzLabel}
          scales={NO_SCALES}
          tolerance={s.display.tolerance}
          theme={theme}
        />
      ) : (
        <p className="muted">Loading run window…</p>
      )}
      <details className="readings">
        <summary>Run metadata</summary>
        <pre className="mono sub">{JSON.stringify(run.metadata, null, 2)}</pre>
      </details>
    </div>
  );
}

function ToleranceView({ t, tz, tzLabel }: { t: SoakTolerance; tz: string; tzLabel: string }) {
  if (!t.soak) return <p className="sub">No soak segment in this run yet.</p>;
  const pct = (v: number | null | undefined) => (typeof v === "number" ? `${(v * 100).toFixed(1)} %` : "—");
  const tol = t.rules.tolerance_c as number;
  return (
    <div data-testid="soak-tolerance">
      <div className="metrics">
        <Metric label={`Within ±${tol} °C`} value={pct(t.within_fraction_of_observed)} hint={`${formatDuration(t.within_s ?? 0)} of observed soak`} />
        <Metric label="Out of tolerance" value={formatDuration(t.out_s ?? 0)} hint={`${t.excursions.length} excursion${t.excursions.length === 1 ? "" : "s"} ≥ ${String(t.rules.min_excursion_s)} s`} />
        <Metric label="Unobserved" value={formatDuration(t.unobserved_s ?? 0)} hint={`coverage ${pct(t.coverage)}`} />
        <Metric label="Soak" value={formatDuration(t.soak.duration_s)} hint={`${formatTime(t.soak.start, tz, false)} – ${formatTime(t.soak.end, tz, false)} ${tzLabel}`} />
      </div>
      {t.excursions.length > 0 && (
        <div className="table-wrap" style={{ marginBottom: 12 }}>
          <table>
            <thead>
              <tr>
                <th scope="col">Excursion ({tzLabel})</th>
                <th scope="col">Direction</th>
                <th scope="col">Duration</th>
                <th scope="col">Max deviation</th>
              </tr>
            </thead>
            <tbody>
              {t.excursions.map((e) => (
                <tr key={e.start}>
                  <td className="mono sub">
                    {formatTime(e.start, tz, false)} – {formatTime(e.end, tz, false)}
                  </td>
                  <td>{e.direction === "above" ? "▲ above" : "▼ below"}</td>
                  <td className="num">{formatDuration(e.duration_s)}</td>
                  <td className="num">
                    {e.max_deviation_c > 0 ? "+" : ""}
                    {e.max_deviation_c.toFixed(2)} °C at {formatTime(e.at, tz, false)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="sub">
        Rules: process value and recorded setpoint paired by acquisition frame during the Soak phase; each sample holds until the next for at most {String(t.rules.max_hold_s)} s; gaps and bad samples are unobserved; excursions separated by less than {String(t.rules.merge_dwell_s)} s merge.
      </p>
    </div>
  );
}

function Runs({ s, tz, tzLabel, theme, canRecord }: Common & { s: InstrumentStatus }) {
  const [creating, setCreating] = useState(false);
  const runs = usePolling((signal) => api.get<{ items: RunView[] }>(`/api/v1/instruments/${encodeURIComponent(s.instrument.id)}/runs?limit=50`, signal), 10000, `runs:${s.instrument.id}`);
  const items = runs.data?.items ?? [];
  const [selected, setSelected] = useState<string | null>(null);
  const current = selected ?? items.find((r) => !r.active)?.id ?? items[0]?.id ?? null;
  const detail = usePolling(
    current ? (signal) => api.get<RunDetail>(`/api/v1/runs/${encodeURIComponent(current)}`, signal) : null,
    items.find((r) => r.id === current)?.active ? 5000 : 600_000,
    `run:${current}`,
  );
  if (runs.loading && !runs.data) return <p className="muted">Loading runs…</p>;
  const toolbar = (
    <div className="toolbar">
      <span className="sub">{items.length} runs · instrument-reported and user records</span>
      <span className="spacer" />
      {canRecord && (
        <button type="button" className="btn-primary" onClick={() => setCreating(!creating)} aria-expanded={creating} data-testid="new-run">
          ＋ New run record
        </button>
      )}
    </div>
  );
  const form = creating && (
    <NewRunForm
      instrumentId={s.instrument.id}
      tz={tz}
      tzLabel={tzLabel}
      onCancel={() => setCreating(false)}
      onCreated={(r) => {
        setCreating(false);
        setSelected(r.id);
        runs.refresh();
      }}
    />
  );
  if (items.length === 0)
    return (
      <>
        {toolbar}
        {form}
        <p className="empty">No runs recorded yet for this instrument.</p>
      </>
    );
  return (
    <>
    {toolbar}
    {form}
    <div className="split">
      <div className="card table-wrap">
        <table data-testid="runs-table">
          <thead>
            <tr>
              <th scope="col">Run</th>
              <th scope="col">Started ({tzLabel})</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.id} className={r.id === current ? "selected" : ""}>
                <td>
                  <button type="button" className="btn-ghost" style={{ padding: 0, color: "var(--accent)" }} onClick={() => setSelected(r.id)} aria-pressed={r.id === current}>
                    {r.name}
                  </button>
                  <div className="sub">{humanize(r.kind)}</div>
                </td>
                <td className="mono sub">{formatTime(r.started_at, tz)}</td>
                <td>
                  <RunBadge active={r.active} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div>
        {detail.error && <p className="error">Could not load the run: {detail.error.message}</p>}
        {detail.data ? (
          <RunAnalysisView
            detail={detail.data}
            s={s}
            tz={tz}
            tzLabel={tzLabel}
            theme={theme}
            canRecord={canRecord}
            onChanged={() => {
              detail.refresh();
              runs.refresh();
            }}
          />
        ) : (
          <p className="muted">Loading run…</p>
        )}
      </div>
    </div>
    </>
  );
}

// --- Diagnostics ----------------------------------------------------------------------------

function Diagnostics({ s, drift, tz, tzLabel }: Common & { s: InstrumentStatus; drift: number }) {
  const events = usePolling((signal) => api.get<{ items: AcqEvent[] }>(`/api/v1/instruments/${encodeURIComponent(s.instrument.id)}/events?limit=100`, signal), 10000, `events:${s.instrument.id}`);
  const c = s.collector;
  return (
    <div className="split">
      <div className="card card-pad" data-testid="diagnostics">
        <h2 className="section-title">Collector and acquisition</h2>
        <dl className="kv">
          <dt>Collector</dt>
          <dd>{c.name ?? c.id ?? "none assigned"}</dd>
          <dt>Connectivity</dt>
          <dd><CollectorBadge state={c.connectivity} /></dd>
          <dt>Last heartbeat</dt>
          <dd>{c.last_heartbeat_at ? `${formatTime(c.last_heartbeat_at, tz)} (${formatAge((c.heartbeat_age_s ?? 0) + drift)})` : "never"}</dd>
          <dt>Clock offset</dt>
          <dd>{typeof c.clock_offset_s === "number" ? `${c.clock_offset_s.toFixed(2)} s (collector − server)` : "—"}</dd>
          <dt>Pending upload</dt>
          <dd>{typeof c.unacknowledged === "number" ? `${c.unacknowledged} records` : "—"}</dd>
          <dt>Upload fault</dt>
          <dd>{c.upload_fault ?? "none"}</dd>
          <dt>Acquisition</dt>
          <dd><AcqBadge state={s.acquisition.state} /> <span className="sub">{acquisitionReason(s, tz)}</span></dd>
          <dt>Profile</dt>
          <dd>{s.display.title ?? s.instrument.profile_key} · revision {s.display.profile_revision ?? "—"}</dd>
          {s.display.cadence_note && (
            <>
              <dt>Cadence</dt>
              <dd>{s.display.cadence_note}</dd>
            </>
          )}
          {s.display.sign_convention && (
            <>
              <dt>Sign convention</dt>
              <dd>{s.display.sign_convention}</dd>
            </>
          )}
          <dt>Channels</dt>
          <dd>
            {s.channels.map((ch) => (
              <div key={ch.id} className="sub">
                {ch.name} · {ch.unit || "state"} · every {ch.expected_cadence_s} s · fresh within {ch.freshness_window_s} s
              </div>
            ))}
          </dd>
        </dl>
      </div>
      <div className="card table-wrap">
        <table data-testid="events-table">
          <thead>
            <tr>
              <th scope="col">Captured ({tzLabel})</th>
              <th scope="col">Event</th>
              <th scope="col">Detail</th>
            </tr>
          </thead>
          <tbody>
            {(events.data?.items ?? []).map((e) => (
              <tr key={e.identity}>
                <td className="mono sub">{formatTime(e.at, tz, true)}</td>
                <td>{humanize(e.type)}</td>
                <td className="sub">
                  {e.detail
                    ? Object.entries(e.detail)
                        .filter(([k]) => k !== "metadata")
                        .map(([k, v]) => `${humanize(k)}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
                        .join(" · ")
                    : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {events.data && events.data.items.length === 0 && <p className="empty">No acquisition events yet.</p>}
      </div>
    </div>
  );
}

// --- page -----------------------------------------------------------------------------------

export function InstrumentPage({ id, tz, tzLabel, online, theme, labs }: Common & { id: string; online: boolean; labs: Record<string, string> }) {
  const now = useNow(1000);
  const [tab, setTab] = useState<Tab>("overview");
  const status = usePolling((signal) => api.get<InstrumentStatus>(`/api/v1/instruments/${encodeURIComponent(id)}/status`, signal), 2000, `status:${id}`);
  if (status.error instanceof ApiError && status.error.status === 404) {
    return (
      <main>
        <p className="empty">Instrument not found or not visible to your account.</p>
        <a href="#/">← Back to the fleet</a>
      </main>
    );
  }
  const s = status.data;
  const drift = status.lastSuccessAt ? (now - status.lastSuccessAt) / 1000 : 0;
  const tabs: { key: Tab; label: string }[] = [
    { key: "overview", label: "Overview" },
    { key: "runs", label: s?.display.analysis ? "Runs & XY analysis" : "Runs" },
    { key: "diagnostics", label: "Diagnostics" },
  ];
  return (
    <main>
      <ConnectionBanner online={online} failing={status.failures > 0} lastSuccessAt={status.lastSuccessAt} now={now} tz={tz} tzLabel={tzLabel} />
      <p className="sub" style={{ margin: "0 0 10px" }}>
        <a href="#/">← Fleet overview</a>
      </p>
      {!s ? (
        <p className="muted">Loading instrument…</p>
      ) : (
        <>
          <header className="card ws-head" data-testid="instrument-header">
            <div>
              <div className="row">
                <h1>{s.instrument.name}</h1>
                <SimBadge simulated={s.instrument.simulated} />
              </div>
              <div className="sub">
                {s.instrument.id} · {s.instrument.lab_name}
                {s.instrument.location ? ` · ${s.instrument.location}` : ""}
                {s.instrument.asset_id ? ` · asset ${s.instrument.asset_id}` : ""} · {s.instrument.lifecycle}
              </div>
              {s.active_run && (
                <div className="sub" style={{ marginTop: 6 }}>
                  <span className="pill tone-ok">
                    <span className="dot" aria-hidden="true" /> {s.active_run.name}
                  </span>{" "}
                  since {formatTime(s.active_run.started_at, tz, false)}
                </div>
              )}
            </div>
            <div className="ws-status">
              <div className="row">
                <AcqBadge state={s.acquisition.state} />
                <CollectorBadge state={s.collector.connectivity} />
              </div>
              <div className="sub" data-testid="acq-reason">
                {acquisitionReason(s, tz)}
              </div>
              <div className="sub">
                Process state:{" "}
                <strong data-testid="process-state">{s.process_state.state === "unknown" ? "Unknown" : s.process_state.state}</strong>
                {s.process_state.source ? ` (from ${s.process_state.source.replace("channel:", "")} channel)` : " (no validated state channel)"}
                {s.process_state.state === "unknown" && s.process_state.last_known && ` · last known ${s.process_state.last_known}`}
              </div>
              <div className="sub">
                Heartbeat {s.collector.last_heartbeat_at ? formatAge((s.collector.heartbeat_age_s ?? 0) + drift) : "never"}
                {typeof s.collector.unacknowledged === "number" && ` · ${s.collector.unacknowledged} pending upload`}
                {s.collector.upload_fault && <strong> · upload fault: {s.collector.upload_fault}</strong>}
              </div>
            </div>
          </header>
          <div className="tabs" role="tablist" aria-label="Instrument views">
            {tabs.map((t) => (
              <button key={t.key} type="button" role="tab" aria-selected={tab === t.key} onClick={() => setTab(t.key)} data-testid={`tab-${t.key}`}>
                {t.label}
              </button>
            ))}
          </div>
          {tab === "overview" && <Overview s={s} drift={drift} tz={tz} tzLabel={tzLabel} theme={theme} canRecord={RECORD_ROLES.has(labs[s.instrument.lab_id] ?? "")} />}
          {tab === "runs" && <Runs s={s} tz={tz} tzLabel={tzLabel} theme={theme} canRecord={RECORD_ROLES.has(labs[s.instrument.lab_id] ?? "")} />}
          {tab === "diagnostics" && <Diagnostics s={s} drift={drift} tz={tz} tzLabel={tzLabel} theme={theme} />}
        </>
      )}
    </main>
  );
}
