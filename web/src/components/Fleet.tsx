import { useMemo, useState } from "react";
import { api } from "../api";
import { useNow, usePolling } from "../hooks";
import { acquisitionReason, ACQ_LABEL, formatValue, primaryChannel } from "../status";
import { formatAge, formatTime } from "../time";
import type { AcquisitionState, InstrumentStatus } from "../types";
import { AcqBadge, CollectorBadge, SimBadge } from "./Badges";
import { ConnectionBanner } from "./ConnectionBanner";

type Filter = "all" | AcquisitionState | "offline";

const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "receiving", label: "Receiving" },
  { key: "degraded", label: "Degraded" },
  { key: "stale", label: "Stale" },
  { key: "source_unavailable", label: "Source unavailable" },
  { key: "offline", label: "Collector offline" },
  { key: "waiting", label: "Waiting" },
];

function matches(s: InstrumentStatus, f: Filter): boolean {
  if (f === "all") return true;
  if (f === "offline") return s.collector.connectivity === "offline";
  return s.acquisition.state === f;
}

export function Fleet(props: { tz: string; tzLabel: string; online: boolean }) {
  const { tz, tzLabel, online } = props;
  const poll = usePolling(
    (signal) => api.get<{ server_time: string; items: InstrumentStatus[] }>("/api/v1/instruments", signal),
    5000,
    "fleet",
  );
  const now = useNow(1000);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const items = poll.data?.items ?? [];

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: items.length };
    for (const f of FILTERS.slice(1)) c[f.key] = items.filter((s) => matches(s, f.key)).length;
    return c;
  }, [items]);

  const shown = items.filter((s) => {
    const q = query.trim().toLowerCase();
    const hay = [s.instrument.name, s.instrument.id, s.instrument.asset_id, s.instrument.lab_name]
      .join(" ")
      .toLowerCase();
    return matches(s, filter) && (!q || hay.includes(q));
  });
  // ages tick locally between polls from the server-provided snapshot
  const drift = poll.lastSuccessAt ? (now - poll.lastSuccessAt) / 1000 : 0;

  return (
    <main>
      <ConnectionBanner online={online} failing={poll.failures > 0} lastSuccessAt={poll.lastSuccessAt} now={now} tz={tz} tzLabel={tzLabel} />
      <div className="toolbar">
        <h1>Instruments</h1>
        <label className="search">
          <span className="sr-only">Search instruments</span>
          <input type="search" placeholder="Search name, asset ID or lab" value={query} onChange={(e) => setQuery(e.target.value)} />
        </label>
      </div>
      <div className="filters" role="group" aria-label="Filter by status">
        {FILTERS.map((f) => (
          <button key={f.key} type="button" className={filter === f.key ? "chip active" : "chip"} aria-pressed={filter === f.key} onClick={() => setFilter(f.key)}>
            {f.label} <span className="count">{counts[f.key] ?? 0}</span>
          </button>
        ))}
      </div>
      {poll.loading && !poll.data ? (
        <p className="muted">Loading instruments…</p>
      ) : items.length === 0 ? (
        <p className="empty">No instruments are visible to your account.</p>
      ) : (
        <div className="table-wrap">
          <table className="fleet" data-testid="fleet-table">
            <thead>
              <tr>
                <th scope="col">Instrument</th>
                <th scope="col">Acquisition</th>
                <th scope="col">Last reading</th>
                <th scope="col">Last sample ({tzLabel})</th>
                <th scope="col">Process state</th>
                <th scope="col">
                  <span className="sr-only">Action</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {shown.map((s) => {
                const ch = primaryChannel(s);
                const latest = ch?.latest ?? null;
                return (
                  <tr key={s.instrument.id} data-testid={`fleet-row-${s.instrument.id}`}>
                    <td>
                      <a href={`#/instrument/${encodeURIComponent(s.instrument.id)}`} className="name">
                        {s.instrument.name}
                      </a>
                      <div className="sub">
                        {s.instrument.id} · {s.instrument.lab_name} <SimBadge simulated={s.instrument.simulated} />
                      </div>
                    </td>
                    <td>
                      <AcqBadge state={s.acquisition.state} /> <CollectorBadge state={s.collector.connectivity} />
                      <div className="sub">{acquisitionReason(s, tz)}</div>
                    </td>
                    <td className="num">
                      {ch ? formatValue(ch, latest) : "—"}
                      {ch && <div className="sub">{ch.name}</div>}
                    </td>
                    <td>
                      {latest ? formatTime(latest.event_time, tz) : "—"}
                      <div className="sub">{latest ? formatAge(latest.age_s + drift) : "no samples"}</div>
                    </td>
                    <td>
                      {s.process_state.state === "unknown" ? <span className="muted">Unknown</span> : s.process_state.state}
                      {s.process_state.source && <div className="sub">from {s.process_state.source.replace("channel:", "")} channel</div>}
                    </td>
                    <td>
                      <a className="button-link" href={`#/instrument/${encodeURIComponent(s.instrument.id)}`} aria-label={`Open ${s.instrument.name}`}>
                        Open →
                      </a>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {shown.length === 0 && <p className="empty">No instruments match this filter.</p>}
        </div>
      )}
      <p className="legend muted">
        {Object.values(ACQ_LABEL)
          .map((l) => `${l.icon} ${l.text}`)
          .join("  ·  ")}
      </p>
    </main>
  );
}
