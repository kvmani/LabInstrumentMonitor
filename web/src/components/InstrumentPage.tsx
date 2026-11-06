import { useMemo, useState } from "react";
import { api, ApiError, qs } from "../api";
import { useNow, usePolling } from "../hooks";
import { acquisitionReason, formatValue } from "../status";
import { formatAge, formatDuration, formatTime, fromLocalInput, isoUtc, toLocalInput } from "../time";
import type { ChannelStatus, InstrumentStatus, Series } from "../types";
import { AcqBadge, CollectorBadge, SimBadge } from "./Badges";
import { ConnectionBanner } from "./ConnectionBanner";
import { HistoryChart, type YScale } from "./HistoryChart";

const PRESETS = [
  { key: "15m", label: "15 min", ms: 15 * 60_000 },
  { key: "1h", label: "1 h", ms: 3600_000 },
  { key: "24h", label: "24 h", ms: 24 * 3600_000 },
  { key: "7d", label: "7 d", ms: 7 * 86400_000 },
  { key: "30d", label: "30 d", ms: 30 * 86400_000 },
] as const;

type View =
  | { kind: "live"; preset: (typeof PRESETS)[number]["key"] }
  | { kind: "fixed"; start: number; end: number; reason: string };

function spanOf(view: View): number {
  if (view.kind === "fixed") return view.end - view.start;
  return PRESETS.find((p) => p.key === view.preset)!.ms;
}

function liveInterval(ms: number): number {
  if (ms <= 3600_000) return 2000;
  if (ms <= 86400_000) return 10000;
  return 30000;
}

function Tile({ ch, tz, drift }: { ch: ChannelStatus; tz: string; drift: number }) {
  const latest = ch.latest;
  const good = ch.last_good;
  const showGood = good && latest && good.event_time !== latest.event_time;
  return (
    <div className={`tile ${ch.fresh ? "" : "tile-stale"}`} data-testid={`tile-${ch.key}`}>
      <div className="tile-name">
        {ch.name}
      </div>
      <div className={`tile-value ${latest?.quality === "bad" ? "q-bad" : ""}`}>{formatValue(ch, latest)}</div>
      <div className="sub">
        {latest ? (
          <>
            {formatTime(latest.event_time, tz, false)} · {formatAge(latest.age_s + drift)}
            {!ch.fresh && <strong> · not fresh (window {ch.freshness_window_s} s)</strong>}
            {latest.quality === "uncertain" && <strong> · uncertain</strong>}
          </>
        ) : (
          "no samples"
        )}
      </div>
      {showGood && (
        <div className="sub">
          last good {formatValue(ch, good)} at {formatTime(good.event_time, tz, false)}
        </div>
      )}
    </div>
  );
}

function Summary({ s, tz, tzLabel }: { s: Series; tz: string; tzLabel: string }) {
  const prec = s.channel.display_precision;
  const unit = s.channel.unit;
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
  return (
    <p className="summary" data-testid="series-summary">
      <strong>{s.channel.name}:</strong> {n.toLocaleString()} samples ({valid.toLocaleString()} valid
      {n - valid > 0 ? `, ${(n - valid).toLocaleString()} bad` : ""})
      {max && (
        <>
          {" "}
          · max {max[0].toFixed(prec)} {unit} at {formatTime(max[1], tz, true)} {tzLabel}
        </>
      )}
      {min && (
        <>
          {" "}
          · min {min[0].toFixed(prec)} {unit} at {formatTime(min[1], tz, true)}
        </>
      )}{" "}
      · {s.gaps.length === 0 ? "no gaps" : `${s.gaps.length} gap${s.gaps.length > 1 ? "s" : ""} (${formatDuration(gapTotal)} without data)`} ·{" "}
      {s.mode === "raw" ? "raw samples" : `aggregated in ${s.bucket_s} s buckets (band = min–max, line = mean)`}
    </p>
  );
}

function ReadingsTable({ s, tz, tzLabel }: { s: Series; tz: string; tzLabel: string }) {
  const rows = s.mode === "raw" ? (s.points ?? []).slice(-100).reverse() : [];
  return (
    <details className="readings">
      <summary>Readings table ({s.mode === "raw" ? "latest 100 raw samples" : "buckets"})</summary>
      {s.mode === "raw" ? (
        <table>
          <thead>
            <tr>
              <th scope="col">Time ({tzLabel})</th>
              <th scope="col">{s.channel.name} ({s.channel.unit})</th>
              <th scope="col">Quality</th>
              <th scope="col">Clock</th>
              <th scope="col">Identity</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p[5]}>
                <td>{formatTime(p[0], tz, true, true)}</td>
                <td className="num">{p[1] === null ? "—" : p[1].toFixed(s.channel.display_precision)}</td>
                <td>{p[2]}{p[3] ? ` (${p[3].replace(/_/g, " ")})` : ""}</td>
                <td>{p[4]}</td>
                <td className="sub">{p[5]}</td>
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
                <td>{formatTime(b.t, tz, true)}</td>
                <td className="num">{b.min?.toFixed(s.channel.display_precision) ?? "—"}</td>
                <td className="num">{b.mean?.toFixed(s.channel.display_precision) ?? "—"}</td>
                <td className="num">{b.max?.toFixed(s.channel.display_precision) ?? "—"}</td>
                <td>{b.valid_count} / {b.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </details>
  );
}

export function InstrumentPage(props: { id: string; tz: string; tzLabel: string; online: boolean }) {
  const { id, tz, tzLabel, online } = props;
  const now = useNow(1000);
  const status = usePolling(
    (signal) => api.get<InstrumentStatus>(`/api/v1/instruments/${encodeURIComponent(id)}/status`, signal),
    2000,
    `status:${id}`,
  );
  const [view, setView] = useState<View>({ kind: "live", preset: "1h" });
  const [yScale, setYScale] = useState<YScale>({ mode: "auto", min: 0, max: 1000 });
  const [custom, setCustom] = useState<{ start: string; end: string } | null>(null);
  const [rangeError, setRangeError] = useState<string | null>(null);

  const channels = status.data?.channels ?? [];
  const measuredCh = channels.find((c) => c.role === "measurement");
  const setpointCh = channels.find((c) => c.role === "setpoint");
  const stateCh = channels.find((c) => c.role === "state");
  const plotted = [measuredCh, setpointCh, stateCh].filter(Boolean) as ChannelStatus[];
  const viewKey = view.kind === "live" ? `live:${view.preset}` : `fixed:${view.start}:${view.end}`;
  const span = spanOf(view);

  const history = usePolling<{ start: number; end: number; series: Series[] }>(
    plotted.length
      ? async (signal) => {
          const end = view.kind === "live" ? Date.now() + 1000 : view.end;
          const start = view.kind === "live" ? end - span : view.start;
          const width = Math.min(1200, Math.max(300, Math.round(window.innerWidth * 0.9)));
          const series = await Promise.all(
            plotted.map((c) =>
              api.get<Series>(
                `/api/v1/channels/${encodeURIComponent(c.id)}/series?${qs({ start: isoUtc(start), end: isoUtc(end), max_points: width })}`,
                signal,
              ),
            ),
          );
          return { start, end, series };
        }
      : null,
    view.kind === "live" ? liveInterval(span) : 15000,
    `${id}|${viewKey}|${plotted.map((c) => c.id).join(",")}`,
  );

  const bySeries = useMemo(() => {
    const m = new Map<string, Series>();
    for (const s of history.data?.series ?? []) m.set(s.channel.id, s);
    return m;
  }, [history.data]);

  if (status.error instanceof ApiError && status.error.status === 404) {
    return (
      <main>
        <p className="empty">Instrument not found or not visible to your account.</p>
        <a href="#/">← Back to instruments</a>
      </main>
    );
  }
  const s = status.data;
  const drift = status.lastSuccessAt ? (now - status.lastSuccessAt) / 1000 : 0;
  const range = history.data ? { start: history.data.start, end: history.data.end } : null;
  const csvHref = range
    ? `/api/v1/instruments/${encodeURIComponent(id)}/export.csv?${qs({ start: isoUtc(range.start), end: isoUtc(range.end) })}`
    : undefined;

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

  const title = s
    ? `${s.instrument.name}${s.instrument.simulated ? " [SIMULATED]" : ""} · ${range ? `${formatTime(range.start, tz)} – ${formatTime(range.end, tz)} ${tzLabel}` : ""}`
    : "";

  return (
    <main>
      <ConnectionBanner online={online} failing={status.failures > 0} lastSuccessAt={status.lastSuccessAt} now={now} tz={tz} tzLabel={tzLabel} />
      <p>
        <a href="#/">← Instruments</a>
      </p>
      {!s ? (
        <p className="muted">Loading instrument…</p>
      ) : (
        <>
          <header className="inst-header" data-testid="instrument-header">
            <div>
              <h1>
                {s.instrument.name} <SimBadge simulated={s.instrument.simulated} />
              </h1>
              <div className="sub">
                {s.instrument.id} · {s.instrument.lab_name}
                {s.instrument.location ? ` · ${s.instrument.location}` : ""}
                {s.instrument.asset_id ? ` · asset ${s.instrument.asset_id}` : ""} · lifecycle {s.instrument.lifecycle}
              </div>
            </div>
            <div className="inst-status">
              <div>
                <AcqBadge state={s.acquisition.state} /> <CollectorBadge state={s.collector.connectivity} />
              </div>
              <div className="sub" data-testid="acq-reason">{acquisitionReason(s, tz)}</div>
              <div className="sub">
                Process state:{" "}
                <strong data-testid="process-state">{s.process_state.state === "unknown" ? "Unknown" : s.process_state.state}</strong>
                {s.process_state.source && ` (from ${s.process_state.source.replace("channel:", "")} channel)`}
                {s.process_state.state === "unknown" && s.process_state.last_known && ` · last known ${s.process_state.last_known}`}
              </div>
              <div className="sub">
                Collector heartbeat {s.collector.last_heartbeat_at ? formatAge((s.collector.heartbeat_age_s ?? 0) + drift) : "never"}
                {typeof s.collector.unacknowledged === "number" && ` · ${s.collector.unacknowledged} pending upload`}
                {s.collector.upload_fault && <strong> · upload fault: {s.collector.upload_fault}</strong>}
              </div>
            </div>
          </header>

          <section className="tiles" aria-label="Latest readings">
            {s.channels.map((c) => (
              <Tile key={c.id} ch={c} tz={tz} drift={drift} />
            ))}
          </section>

          <section className="history" aria-labelledby="history-title">
            <div className="history-bar">
              <h2 id="history-title">History</h2>
              <div role="group" aria-label="Time range" className="presets">
                {PRESETS.map((p) => (
                  <button key={p.key} type="button" className={view.kind === "live" && view.preset === p.key ? "chip active" : "chip"} aria-pressed={view.kind === "live" && view.preset === p.key} onClick={() => setView({ kind: "live", preset: p.key })}>
                    {p.label}
                  </button>
                ))}
                <button type="button" className={custom ? "chip active" : "chip"} onClick={() => setCustom(custom ?? { start: toLocalInput((range?.start ?? Date.now() - 3600_000), tz), end: toLocalInput((range?.end ?? Date.now()), tz) })}>
                  Custom…
                </button>
              </div>
              <div className="live-state" data-testid="live-state">
                {view.kind === "live" ? (
                  <span className="badge tone-ok">● Live — following latest</span>
                ) : (
                  <>
                    <span className="badge tone-muted">‖ Paused ({view.reason})</span>{" "}
                    <button type="button" onClick={() => setView({ kind: "live", preset: "1h" })}>
                      Resume live
                    </button>
                  </>
                )}
              </div>
            </div>
            {custom && (
              <div className="custom-range">
                <label>
                  Start ({tzLabel}) <input type="datetime-local" value={custom.start} onChange={(e) => setCustom({ ...custom, start: e.target.value })} />
                </label>
                <label>
                  End ({tzLabel}) <input type="datetime-local" value={custom.end} onChange={(e) => setCustom({ ...custom, end: e.target.value })} />
                </label>
                <button type="button" onClick={applyCustom}>
                  Apply range
                </button>
                {rangeError && <span className="error" role="alert">{rangeError}</span>}
              </div>
            )}
            <div className="chart-tools">
              <span role="group" aria-label="Zoom and pan">
                <button type="button" onClick={() => pan(-1)} aria-label="Pan earlier">◀</button>
                <button type="button" onClick={() => zoom(0.5)}>Zoom in</button>
                <button type="button" onClick={() => zoom(2)}>Zoom out</button>
                <button type="button" onClick={() => pan(1)} aria-label="Pan later">▶</button>
              </span>
              <span role="group" aria-label="Temperature axis">
                <label>
                  <input type="checkbox" checked={yScale.mode === "auto"} onChange={(e) => setYScale({ ...yScale, mode: e.target.checked ? "auto" : "fixed" })} /> Auto Y
                </label>
                {yScale.mode === "fixed" && (
                  <>
                    <label>
                      min <input type="number" value={yScale.min} onChange={(e) => setYScale({ ...yScale, min: Number(e.target.value) })} className="narrow" />
                    </label>
                    <label>
                      max <input type="number" value={yScale.max} onChange={(e) => setYScale({ ...yScale, max: Number(e.target.value) })} className="narrow" />
                    </label>
                  </>
                )}
              </span>
              {csvHref && (
                <a className="button-link" href={csvHref} download data-testid="csv-link">
                  Download CSV (raw rows)
                </a>
              )}
              <span className="sub">Shift + wheel on the chart zooms; the view then pauses.</span>
            </div>
            {history.error && <p className="error" role="alert">History request failed: {history.error.message}. Retrying…</p>}
            {history.loading && !history.data && <p className="muted">Loading history…</p>}
            {range && measuredCh && (
              <>
                <HistoryChart
                  title={title}
                  measured={bySeries.get(measuredCh.id)}
                  setpoint={setpointCh ? bySeries.get(setpointCh.id) : undefined}
                  state={stateCh ? bySeries.get(stateCh.id) : undefined}
                  start={range.start}
                  end={range.end}
                  tz={tz}
                  tzLabel={tzLabel}
                  yScale={yScale}
                  onZoom={(a, b) => pause(a, b, "zoomed")}
                />
                {bySeries.get(measuredCh.id) && (
                  <>
                    <Summary s={bySeries.get(measuredCh.id)!} tz={tz} tzLabel={tzLabel} />
                    <ReadingsTable s={bySeries.get(measuredCh.id)!} tz={tz} tzLabel={tzLabel} />
                  </>
                )}
              </>
            )}
          </section>
        </>
      )}
    </main>
  );
}
