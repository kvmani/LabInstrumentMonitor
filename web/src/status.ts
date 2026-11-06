import type { AcquisitionState, ChannelStatus, InstrumentStatus } from "./types";
import { formatTime } from "./time";

export const ACQ_LABEL: Record<AcquisitionState, { text: string; icon: string; tone: string }> = {
  receiving: { text: "Receiving", icon: "●", tone: "ok" },
  degraded: { text: "Degraded", icon: "▲", tone: "warn" },
  stale: { text: "Stale", icon: "◌", tone: "stale" },
  source_unavailable: { text: "Source unavailable", icon: "✕", tone: "bad" },
  waiting: { text: "Waiting", icon: "…", tone: "muted" },
  paused: { text: "Paused", icon: "‖", tone: "muted" },
};

export const COLLECTOR_LABEL: Record<string, { text: string; icon: string; tone: string }> = {
  online: { text: "Collector online", icon: "●", tone: "ok" },
  offline: { text: "Collector offline", icon: "✕", tone: "bad" },
  never_seen: { text: "Collector never seen", icon: "?", tone: "muted" },
  revoked: { text: "Collector revoked", icon: "⊘", tone: "bad" },
};

export function acquisitionReason(s: InstrumentStatus, tz: string): string {
  const a = s.acquisition;
  const at = (iso: string | null | undefined) => (iso ? formatTime(iso, tz, true) : "—");
  switch (a.reason_code) {
    case "fresh":
      return "fresh readings from the current collector";
    case "bad_quality":
      return `bad readings: ${(a.channels ?? [])
        .map((c) => `${channelName(s, c.key)} (${(c.quality_reason ?? "bad").replace(/_/g, " ")})`)
        .join(", ")}`;
    case "partial":
      return `no recent readings on ${(a.channels ?? []).map((c) => channelName(s, c.key)).join(", ")}`;
    case "collector_online_no_response":
    case "no_response":
      return "collector online · no response from controller";
    case "collector_sampling_delivery_delayed":
      return `collector sampling · delivery delayed (${a.unacknowledged ?? "?"} records pending)`;
    case "collector_online_no_new_readings":
      return `collector online · no new readings since ${at(a.last_good_at)}`;
    case "collector_offline":
      return `collector offline · last good reading at ${at(a.last_good_at)}`;
    case "collector_revoked":
      return "collector credential revoked";
    case "collector_never_seen":
      return "collector has never connected";
    case "collector_online_no_samples":
      return "collector online · waiting for the first committed reading";
    case "no_samples_yet":
      return "no committed readings yet";
    case "no_assignment":
      return "no collector assigned";
    default:
      return a.reason_code.replace(/_/g, " ");
  }
}

function channelName(s: InstrumentStatus, key: string): string {
  return s.channels.find((c) => c.key === key)?.name ?? key;
}

export function primaryChannel(s: InstrumentStatus): ChannelStatus | undefined {
  const key = s.display?.panels?.[0]?.channels?.[0];
  return (
    s.channels.find((c) => c.key === key) ??
    s.channels.find((c) => c.role === "measurement") ??
    s.channels[0]
  );
}

export function needsAttention(s: InstrumentStatus): boolean {
  return ["degraded", "stale", "source_unavailable"].includes(s.acquisition.state);
}
