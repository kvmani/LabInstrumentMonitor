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
  faults: Fault[];
}

export interface Fault {
  severity: "critical" | "warning" | "info";
  code: string;
  message: string;
  next_step: string;
}

export interface SystemHealth {
  server_time: string;
  rollup_queue: { minutes: number; oldest_marked_at: string | null };
  export_jobs: Record<string, number>;
  database_bytes: number;
  export_disk: { free_bytes: number; total_bytes: number } | null;
  tls: { status: "ok" | "warn" | "fail"; detail: string; days_left?: number } | null;
  faults: Fault[];
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

export interface Excursion {
  start: string;
  end: string;
  duration_s: number;
  direction: "above" | "below";
  max_deviation_c: number;
  at: string;
}

export interface SoakTolerance {
  kind: "soak_tolerance";
  rules: Record<string, unknown>;
  soak: { start: string; end: string; duration_s: number } | null;
  within_s?: number;
  out_s?: number;
  unobserved_s?: number;
  coverage?: number | null;
  within_fraction_of_observed?: number | null;
  excursions: Excursion[];
}

export interface RunDetail {
  server_time: string;
  run: RunView;
  display: Display;
  analysis: XYAnalysis | null;
  tolerance: SoakTolerance | null;
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

// --- commissioning (ONB-01) and collectors -------------------------------------------------

export interface ProfileChannel {
  key: string;
  name: string;
  quantity: string;
  unit: string;
  data_type: string;
  role: string;
  expected_cadence_s: number;
  valid_min: number | null;
  valid_max: number | null;
  enum_labels?: Record<string, string> | null;
  note?: string | null;
}

export interface ProfileSummary {
  key: string;
  title: string;
  adapter: string;
  adapter_version: string;
  opt_in: boolean;
  sign_convention: string | null;
  cadence_note: string | null;
  source_time_meaning: string;
  channels: ProfileChannel[];
}

export interface CollectorView {
  id: string;
  lab_id: string;
  name: string;
  revoked: boolean;
  last_heartbeat_at: string | null;
  heartbeat_age_s: number | null;
  online: boolean;
  software_version: string | null;
  sampling: boolean | null;
  upload_fault: string | null;
  sources: { instrument_id: string; state: string; profile_revision_id?: string }[];
  assigned_instruments: string[];
  active_credentials: number;
}

export interface CommissioningOptions {
  labs: { id: string; role: string }[];
  can_provision: string[];
  profiles: ProfileSummary[];
  collectors: CollectorView[];
  custodians: Record<string, { username: string; display_name: string }[]>;
  instruments: { id: string; lab_id: string; name: string; profile: string }[];
  steps: string[];
}

export interface DraftData {
  name?: string;
  location?: string;
  asset_id?: string;
  custodian?: string;
  collector_id?: string;
  interface_ref?: string;
  mode?: "simulated";
  profile?: string;
  seed?: number;
  notes?: string;
}

export interface Check {
  name: string;
  ok: boolean;
  detail: string;
}

export interface DraftView {
  id: string;
  lab_id: string;
  instrument_id: string;
  state: "draft" | "published" | "abandoned";
  step: number;
  step_name: string;
  data: DraftData;
  preview: {
    ok: boolean;
    at: string;
    profile: string;
    seed: number;
    checks: Check[];
    stale: boolean;
  } | null;
  missing: string[];
  existing: (DraftData & { revision: number | null; revision_id: string | null }) | null;
  changes: { field: string; before: unknown; after: unknown }[];
  created_by: string | null;
  updated_by: string | null;
  created_at: string;
  updated_at: string;
  published_revision_id: string | null;
  published_at: string | null;
}

export interface PreviewFrame {
  t: string;
  unavailable?: string;
  run?: string;
  channels?: Record<
    string,
    {
      value: number | boolean | null;
      raw: string | null;
      quality: string;
      reason: string | null;
      unit: string;
    }
  >;
}

export interface PreviewResult {
  ok: boolean;
  profile: string;
  seed: number;
  window: { start: string; end: string; frames: number };
  channels: ProfileChannel[];
  frames: PreviewFrame[];
  trend: Record<string, [string, number | null][]>;
  drill: { fault: string; from: string; to: string; unavailable_frames: number };
  checks: Check[];
}

export interface VerifyResult {
  state: "awaiting_collector" | "awaiting_samples" | "receiving";
  revision_id: string;
  collector: {
    id: string;
    heartbeat_age_s: number | null;
    online: boolean;
    applied_revision_id: string | null;
    source_state: string | null;
  };
  samples: { count: number; first_received: string | null; last_received: string | null };
  latest: {
    channel_id: string;
    value: number | boolean | null;
    quality: string;
    event_time: string;
  }[];
  checked_at: string;
}
