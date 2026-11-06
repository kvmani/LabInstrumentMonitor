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
  display_format: "fixed" | "scientific";
  note: string | null;
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
  display: Display;
  active_run: RunView | null;
}

export interface PanelDef {
  key: string;
  title: string;
  channels: string[];
  scale: "linear" | "log";
  log_allowed: boolean;
  tolerance_of: string | null;
}

export interface Display {
  profile_revision_id: string | null;
  profile_revision: number | null;
  title: string | null;
  panels: PanelDef[];
  analysis: "tensile" | "dsc" | null;
  sign_convention: string | null;
  cadence_note: string | null;
  tolerance: number | null;
  state_channel: string | null;
}

export interface RunView {
  id: string;
  instrument_id: string;
  run_key: string;
  name: string;
  kind: string;
  source: string;
  simulated: boolean;
  started_at: string;
  planned_end_at: string | null;
  ended_at: string | null;
  end_reason: string | null;
  active: boolean;
  metadata: Record<string, unknown>;
  notes: string | null;
  tags: string[];
  created_by: string | null;
  updated_at: string | null;
}

export interface AnnotationView {
  id: number;
  instrument_id: string;
  run_id: string | null;
  at: string;
  end_at: string | null;
  text: string;
  author: string | null;
  revision: number;
  created_at: string;
  updated_at: string | null;
  retracted: boolean;
  retract_reason: string | null;
}

export interface XYAnalysis {
  kind: "tensile" | "dsc";
  x: { label: string; unit: string; key: string };
  y: { label: string; unit: string; key: string };
  sign_convention?: string | null;
  point_fields: string[];
  /** [x, y, time, state|segment] or null for a break */
  points: ([number, number, string, number | string] | null)[];
  summary: Record<string, unknown>;
  geometry?: Record<string, unknown> | null;
}

export interface RunDetail {
  server_time: string;
  run: RunView;
  display: Display;
  analysis: XYAnalysis | null;
}

export interface AcqEvent {
  type: string;
  at: string;
  received_at: string;
  detail: Record<string, unknown> | null;
  identity: string;
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
    display_format: "fixed" | "scientific";
    note: string | null;
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
  aggregation?: "rollup_1m" | "raw_on_demand";
  rebuilding_minutes?: number;
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

export interface ExportJobView {
  id: string;
  instrument_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "expired";
  params: { start: string; end: string; channels: string[] | null; estimated_rows: number };
  created_at: string;
  finished_at: string | null;
  attempts: number;
  row_count: number | null;
  byte_size: number | null;
  sha256: string | null;
  file_name: string | null;
  expires_at: string | null;
  error: string | null;
}
