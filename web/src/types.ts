export type Quality = "good" | "uncertain" | "bad";

export interface SampleView {
  value: number | boolean | null;
  label: string | null;
  quality: Quality;
  quality_reason: string | null;
  event_time: string;
  age_s: number;
  receive_time: string;
  receive_lag_s: number;
  clock_confidence: string;
}

export interface ChannelStatus {
  id: string;
  key: string;
  name: string;
  quantity: string;
  unit: string;
  data_type: "float" | "enum" | "bool";
  role: "measurement" | "setpoint" | "state";
  display_precision: number;
  enum_labels: Record<string, string> | null;
  expected_cadence_s: number;
  freshness_window_s: number;
  fresh: boolean;
  latest: SampleView | null;
  last_good: SampleView | null;
}

export type AcquisitionState =
  | "waiting"
  | "receiving"
  | "degraded"
  | "stale"
  | "source_unavailable"
  | "paused";

export interface InstrumentStatus {
  server_time: string;
  instrument: {
    id: string;
    name: string;
    lab_id: string;
    lab_name: string;
    asset_id: string | null;
    location: string | null;
    lifecycle: string;
    simulated: boolean;
    profile_key: string;
  };
  collector: {
    id: string | null;
    name?: string;
    connectivity: "never_seen" | "online" | "offline" | "revoked";
    last_heartbeat_at: string | null;
    heartbeat_age_s?: number | null;
    clock_offset_s?: number | null;
    unacknowledged?: number | null;
    upload_fault?: string | null;
  };
  acquisition: {
    state: AcquisitionState;
    reason_code: string;
    last_good_at: string | null;
    unacknowledged?: number;
    channels?: { key: string; quality_reason?: string }[];
  };
  process_state: {
    state: string;
    code?: number;
    source: string | null;
    last_known?: string | null;
    last_known_at?: string | null;
  };
  channels: ChannelStatus[];
}

export interface Gap {
  start: string;
  end: string;
  duration_s: number;
  open: boolean;
}

export interface Bucket {
  t: string;
  count: number;
  valid_count: number;
  uncertain_count: number;
  bad_count: number;
  mean: number | null;
  min: number | null;
  min_t: string | null;
  max: number | null;
  max_t: string | null;
  first: number | null;
  last: number | null;
  first_t: string;
  last_t: string;
}

/** [event_time, value|null, quality, quality_reason, clock_confidence, "epoch:seq"] */
export type RawPoint = [string, number | null, Quality, string | null, string, string];

export interface Series {
  channel: {
    id: string;
    key: string;
    name: string;
    quantity: string;
    unit: string;
    data_type: "float" | "enum" | "bool";
    role: string;
    display_precision: number;
    enum_labels: Record<string, string> | null;
    expected_cadence_s: number;
    gap_threshold_s: number;
  };
  instrument_id: string;
  simulated: boolean;
  range: { start: string; end: string };
  server_time: string;
  sample_count: number;
  data_revision: number | null;
  mode: "raw" | "bucket";
  bucket_s?: number;
  points?: RawPoint[];
  buckets?: Bucket[];
  gaps: Gap[];
  events: { type: string; at: string; detail: Record<string, unknown> | null }[];
}

export interface SessionInfo {
  username: string;
  display_name: string;
  labs: Record<string, string>;
  display_timezone: string;
  environment: string;
}
