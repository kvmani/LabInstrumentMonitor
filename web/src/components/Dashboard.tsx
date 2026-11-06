import { useMemo, useState } from "react";
import { api, qs } from "../api";
import { formatValue } from "../format";
import { useNow, usePolling, useStoredState } from "../hooks";
import { acquisitionReason, needsAttention, primaryChannel } from "../status";
import { formatAge, formatTime, isoUtc } from "../time";
import type { InstrumentStatus, Series } from "../types";
import { AcqBadge, CollectorBadge, SimBadge } from "./Badges";
import { ConnectionBanner } from "./ConnectionBanner";
import { Sparkline } from "./Sparkline";

type Filter = "all" | "receiving" | "attention" | "offline" | "runs";

const SPARK_MS = 15 * 60_000;

function matches(s: InstrumentStatus, f: Filter): boolean {
  switch (f) {
    case "all":
      return true;
    case "receiving":
      return s.acquisition.state === "receiving";
    case "attention":
      return needsAttention(s);
    case "offline":
      return s.collector.connectivity !== "online";
    case "runs":
      return s.active_run !== null;
  }
}

function InstrumentCard({
  s,
  tz,
  drift,
}: {
  s: InstrumentStatus;
  tz: string;
  drift: number;
}) {
  const primary = primaryChannel(s);
  const firstPanel = s.display.panels[0];
  const spark = usePolling<Series>(
    primary
      ? (signal) => {
          const end = Date.now() + 1000;
          return api.get<Series>(
            `/api/v1/channels/${encodeURIComponent(primary.id)}/series?${qs({ start: isoUtc(end - SPARK_MS), end: isoUtc(end), max_points: 60 })}`,
            signal,
          );
        }
      : null,
    15000,
    `spark:${primary?.id ?? "none"}`,
  );
  const secondary = s.channels.filter(
    (c) => c !== primary && c.role !== "state",
  );
  const stateCh = s.channels.find((c) => c.role === "state");
  const latest = primary?.latest ?? null;
  const href = `#/instrument/${encodeURIComponent(s.instrument.id)}`;
  const value = primary ? formatValue(primary, latest, false) : "—";
  return (
    <a
      href={href}
      className={`card icard ${primary?.fresh ? "" : "stale"}`}
      data-testid={`card-${s.instrument.id}`}
      aria-label={`${s.instrument.name}: ${s.acquisition.state}`}
    >
      <div className="icard-head">
        <div>
          <div className="icard-title">{s.instrument.name}</div>
          <div className="sub">
            {s.instrument.id} · {s.instrument.location ?? s.instrument.lab_name}
          </div>
        </div>
        <SimBadge simulated={s.instrument.simulated} />
      </div>
      <div className="row">
        <AcqBadge state={s.acquisition.state} />
        <CollectorBadge state={s.collector.connectivity} />
        {s.faults.filter((f) => f.severity !== "info").length > 0 && (
          <span
            className={`pill ${s.faults.some((f) => f.severity === "critical") ? "tone-bad" : "tone-warn"}`}
            title={s.faults.map((f) => f.message).join("\n")}
          >
            ▲ {s.faults.filter((f) => f.severity !== "info").length} fault
            {s.faults.filter((f) => f.severity !== "info").length > 1
              ? "s"
              : ""}
          </span>
        )}
        {stateCh && (
          <span
            className="pill tone-muted"
            title="From the validated state channel"
          >
            {s.process_state.state === "unknown"
              ? "State unknown"
              : s.process_state.state}
          </span>
        )}
      </div>
      <div>
        <div className="sub">{primary?.name}</div>
        <div className="icard-primary">
          <span className={`big ${latest?.quality === "bad" ? "error" : ""}`}>
            {value}
          </span>
          {latest?.value !== null && primary?.unit && (
            <span className="big-unit">{primary.unit}</span>
          )}
        </div>
        {secondary.length > 0 && (
          <div className="secondary">
            {secondary.map((c) => (
              <span key={c.id}>
                <span className="muted">{c.name}</span>{" "}
                <b>{formatValue(c, c.latest)}</b>
              </span>
            ))}
          </div>
        )}
      </div>
      <Sparkline
        series={spark.data ?? undefined}
        log={firstPanel?.scale === "log"}
        label={`${primary?.name ?? ""} trend`}
      />
      <div className="icard-foot">
        <span>
          {latest
            ? `${formatTime(latest.event_time, tz, false)} · ${formatAge(latest.age_s + drift)}`
            : acquisitionReason(s, tz)}
        </span>
        <span>{s.active_run ? `▶ ${s.active_run.name}` : "no active run"}</span>
      </div>
    </a>
  );
}

export function Dashboard({
  tz,
  tzLabel,
  online,
}: {
  tz: string;
  tzLabel: string;
  online: boolean;
}) {
  const poll = usePolling(
    (signal) =>
      api.get<{ server_time: string; items: InstrumentStatus[] }>(
        "/api/v1/instruments",
        signal,
      ),
    5000,
    "fleet",
  );
  const now = useNow(1000);
  const [view, setView] = useStoredState<"cards" | "table">(
    "labmon.fleetView",
    "cards",
  );
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const items = useMemo(() => poll.data?.items ?? [], [poll.data]);
  const drift = poll.lastSuccessAt ? (now - poll.lastSuccessAt) / 1000 : 0;

  const kpis: { key: Filter; label: string; value: number }[] = [
    { key: "all", label: "Instruments", value: items.length },
    {
      key: "receiving",
      label: "Receiving",
      value: items.filter((s) => matches(s, "receiving")).length,
    },
    {
      key: "attention",
      label: "Need attention",
      value: items.filter((s) => matches(s, "attention")).length,
    },
    {
      key: "offline",
      label: "Collector not online",
      value: items.filter((s) => matches(s, "offline")).length,
    },
    {
      key: "runs",
      label: "Active runs",
      value: items.filter((s) => matches(s, "runs")).length,
    },
  ];
  const q = query.trim().toLowerCase();
  const shown = items.filter(
    (s) =>
      matches(s, filter) &&
      (!q ||
        [
          s.instrument.name,
          s.instrument.id,
          s.instrument.asset_id,
          s.instrument.lab_name,
          s.instrument.location,
        ]
          .join(" ")
          .toLowerCase()
          .includes(q)),
  );

  return (
    <main>
      <ConnectionBanner
        online={online}
        failing={poll.failures > 0}
        lastSuccessAt={poll.lastSuccessAt}
        now={now}
        tz={tz}
        tzLabel={tzLabel}
      />
      <div className="page-head">
        <div>
          <h1>Fleet overview</h1>
          <p className="sub">
            Live acquisition status for every instrument visible to you · times
            in {tzLabel}
          </p>
        </div>
        <span className="spacer" />
        <label>
          <span className="sr-only">Search instruments</span>
          <input
            type="search"
            placeholder="Search name, asset ID, lab or location"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <div className="segmented" role="group" aria-label="Layout">
          <button
            type="button"
            aria-pressed={view === "cards"}
            onClick={() => setView("cards")}
          >
            Cards
          </button>
          <button
            type="button"
            aria-pressed={view === "table"}
            onClick={() => setView("table")}
          >
            Table
          </button>
        </div>
      </div>

      <div className="kpis" role="group" aria-label="Filter by status">
        {kpis.map((k) => (
          <button
            key={k.key}
            type="button"
            className="kpi"
            aria-pressed={filter === k.key}
            onClick={() => setFilter(filter === k.key ? "all" : k.key)}
            data-testid={`kpi-${k.key}`}
          >
            <span className="kpi-value">{k.value}</span>
            <span className="kpi-label">{k.label}</span>
          </button>
        ))}
      </div>

      {poll.loading && !poll.data ? (
        <p className="muted">Loading instruments…</p>
      ) : items.length === 0 ? (
        <p className="empty">No instruments are visible to your account.</p>
      ) : shown.length === 0 ? (
        <p className="empty">No instruments match this filter.</p>
      ) : view === "cards" ? (
        <div className="cards" data-testid="fleet-cards">
          {shown.map((s) => (
            <InstrumentCard key={s.instrument.id} s={s} tz={tz} drift={drift} />
          ))}
        </div>
      ) : (
        <div className="card table-wrap">
          <table data-testid="fleet-table">
            <thead>
              <tr>
                <th scope="col">Instrument</th>
                <th scope="col">Acquisition</th>
                <th scope="col">Last reading</th>
                <th scope="col">Last sample ({tzLabel})</th>
                <th scope="col">Process state</th>
                <th scope="col">Run</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((s) => {
                const ch = primaryChannel(s);
                const latest = ch?.latest ?? null;
                return (
                  <tr
                    key={s.instrument.id}
                    data-testid={`fleet-row-${s.instrument.id}`}
                  >
                    <td>
                      <a
                        href={`#/instrument/${encodeURIComponent(s.instrument.id)}`}
                      >
                        {s.instrument.name}
                      </a>
                      <div className="sub">
                        {s.instrument.id} · {s.instrument.lab_name}{" "}
                        <SimBadge simulated={s.instrument.simulated} />
                      </div>
                    </td>
                    <td>
                      <div className="row">
                        <AcqBadge state={s.acquisition.state} />{" "}
                        <CollectorBadge state={s.collector.connectivity} />
                      </div>
                      <div className="sub">{acquisitionReason(s, tz)}</div>
                    </td>
                    <td className="num">
                      {ch ? formatValue(ch, latest) : "—"}
                      {ch && <div className="sub">{ch.name}</div>}
                    </td>
                    <td>
                      {latest ? formatTime(latest.event_time, tz) : "—"}
                      <div className="sub">
                        {latest
                          ? formatAge(latest.age_s + drift)
                          : "no samples"}
                      </div>
                    </td>
                    <td>
                      {s.process_state.state === "unknown" ? (
                        <span className="muted">Unknown</span>
                      ) : (
                        s.process_state.state
                      )}
                    </td>
                    <td>
                      {s.active_run ? (
                        s.active_run.name
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
