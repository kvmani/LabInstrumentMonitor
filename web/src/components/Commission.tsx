import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { usePolling } from "../hooks";
import { formatTime } from "../time";
import type {
  Check,
  CommissioningOptions,
  DraftData,
  DraftView,
  PreviewResult,
  ProfileSummary,
  VerifyResult,
} from "../types";

/** Roles that commission instruments (SPECIFICATIONS §8). Collectors need administrator. */
export const COMMISSION_ROLES = new Set(["custodian", "administrator"]);
const ID_PATTERN = "[a-z0-9][a-z0-9._-]{0,63}";

function message(e: unknown): string {
  return e instanceof ApiError ? e.message : "request failed";
}

function CheckList({ checks }: { checks: Check[] }) {
  return (
    <ul className="checks" data-testid="checks">
      {checks.map((c) => (
        <li key={c.name} className={c.ok ? "tone-ok" : "tone-bad"}>
          <span aria-hidden="true">{c.ok ? "✓" : "✕"}</span> <strong>{c.name}</strong>{" "}
          <span className="muted">{c.detail}</span>
        </li>
      ))}
    </ul>
  );
}

// --- home: resume a draft or start one ---------------------------------------------------------

export function CommissionHome({ tz }: { tz: string }) {
  const opts = usePolling<CommissioningOptions>(
    (s) => api.get("/api/v1/commissioning/options", s),
    30000,
    "commission-options",
  );
  const drafts = usePolling<{ items: DraftView[] }>(
    (s) => api.get("/api/v1/commissioning/drafts", s),
    30000,
    "commission-drafts",
  );
  const [lab, setLab] = useState("");
  const [instrumentId, setInstrumentId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const labs = opts.data?.labs ?? [];
  const chosenLab = lab || labs[0]?.id || "";
  const existing = (opts.data?.instruments ?? []).filter((i) => i.lab_id === chosenLab);

  const start = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const d = await api.post<DraftView>("/api/v1/commissioning/drafts", {
        lab_id: chosenLab,
        instrument_id: instrumentId.trim(),
      });
      window.location.hash = `#/commission/${d.id}`;
    } catch (err) {
      setError(message(err));
    }
  };

  if (opts.data && labs.length === 0)
    return (
      <main>
        <h1>Commission</h1>
        <p className="empty">Commissioning needs the custodian or administrator role in a lab.</p>
      </main>
    );

  const open = (drafts.data?.items ?? []).filter((d) => d.state === "draft");
  const done = (drafts.data?.items ?? []).filter((d) => d.state === "published");
  return (
    <main data-testid="commission-home">
      <div className="page-head">
        <div>
          <h1>Commission</h1>
          <p className="muted">
            Six steps: identify, connect, map, preview, approve, verify. Drafts are saved on the
            server, so you can stop and resume. Version 1 commissions simulated sources only.
          </p>
        </div>
        <a className="btn" href="#/collectors">
          Collectors
        </a>
      </div>
      <div className="split">
        <form className="card card-pad" onSubmit={start} aria-label="Start commissioning">
          <h2 className="section-title">Start</h2>
          <div className="form-grid">
            <label className="span2">
              Lab
              <select value={chosenLab} onChange={(e) => setLab(e.target.value)}>
                {labs.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.id} ({l.role})
                  </option>
                ))}
              </select>
            </label>
            <label className="span2">
              Instrument id
              <input
                required
                pattern={ID_PATTERN}
                value={instrumentId}
                onChange={(e) => setInstrumentId(e.target.value.toLowerCase())}
                placeholder="e.g. dil-02"
                list="existing-instruments"
                aria-describedby="iid-help"
              />
              <datalist id="existing-instruments">
                {existing.map((i) => (
                  <option key={i.id} value={i.id}>
                    {i.name}
                  </option>
                ))}
              </datalist>
              <span id="iid-help" className="faint">
                A new stable id, or an existing instrument to publish a new revision. Lowercase
                letters, digits, dot, dash, underscore.
              </span>
            </label>
          </div>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <button className="btn btn-primary" type="submit" disabled={!chosenLab}>
            Start draft
          </button>
        </form>
        <section className="card card-pad" aria-label="Drafts">
          <h2 className="section-title">Open drafts</h2>
          {open.length === 0 ? (
            <p className="muted">No open drafts.</p>
          ) : (
            <ul className="ann-list" data-testid="open-drafts">
              {open.map((d) => (
                <li key={d.id}>
                  <a href={`#/commission/${d.id}`}>
                    <strong>{d.data.name || d.instrument_id}</strong>
                  </a>{" "}
                  <span className="mono">{d.instrument_id}</span> · step {d.step} {d.step_name} ·
                  updated {formatTime(d.updated_at, tz)} by {d.updated_by}
                </li>
              ))}
            </ul>
          )}
          {done.length > 0 && (
            <>
              <h2 className="section-title" style={{ marginTop: 16 }}>
                Published
              </h2>
              <ul className="ann-list">
                {done.slice(0, 10).map((d) => (
                  <li key={d.id}>
                    <a href={`#/commission/${d.id}`}>{d.data.name || d.instrument_id}</a>{" "}
                    <span className="mono">{d.published_revision_id}</span> ·{" "}
                    {d.published_at && formatTime(d.published_at, tz)}
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      </div>
    </main>
  );
}

// --- the wizard --------------------------------------------------------------------------------

export function Wizard({ id, tz }: { id: string; tz: string }) {
  const [draft, setDraft] = useState<DraftView | null>(null);
  const [opts, setOpts] = useState<CommissioningOptions | null>(null);
  const [data, setData] = useState<DraftData>({});
  const [step, setStep] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<PreviewResult | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    Promise.all([
      api.get<DraftView>(`/api/v1/commissioning/drafts/${encodeURIComponent(id)}`, ctrl.signal),
      api.get<CommissioningOptions>("/api/v1/commissioning/options", ctrl.signal),
    ])
      .then(([d, o]) => {
        setDraft(d);
        setData(d.data);
        setStep(d.step);
        setOpts(o);
      })
      .catch((e: unknown) => {
        if (!ctrl.signal.aborted) setError(message(e));
      });
    return () => ctrl.abort();
  }, [id]);

  if (error && !draft)
    return (
      <main>
        <p className="banner" role="alert">
          {error}
        </p>
        <a href="#/commission">Back to commissioning</a>
      </main>
    );
  if (!draft || !opts) return <p className="muted pad">Loading…</p>;

  const steps = opts.steps;
  const editable = draft.state === "draft";
  const profile = opts.profiles.find((p) => p.key === data.profile);
  const collectors = opts.collectors.filter((c) => c.lab_id === draft.lab_id && !c.revoked);
  const custodians = opts.custodians[draft.lab_id] ?? [];

  const save = async (next: number) => {
    setError(null);
    setBusy(true);
    try {
      const d = editable
        ? await api.patch<DraftView>(`/api/v1/commissioning/drafts/${draft.id}`, {
            step: next,
            data,
          })
        : draft;
      setDraft(d);
      setData(d.data);
      setStep(next);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };
  const act = async (what: "preview" | "publish" | "abandon") => {
    setError(null);
    setBusy(true);
    try {
      if (what === "preview") {
        await api.patch(`/api/v1/commissioning/drafts/${draft.id}`, { step: 4, data });
        setPreview(
          await api.post<PreviewResult>(`/api/v1/commissioning/drafts/${draft.id}/preview`, {}),
        );
        setDraft(await api.get<DraftView>(`/api/v1/commissioning/drafts/${draft.id}`));
      } else {
        const d = await api.post<DraftView>(`/api/v1/commissioning/drafts/${draft.id}/${what}`, {});
        setDraft(d);
        if (what === "publish") setStep(6);
        if (what === "abandon") window.location.hash = "#/commission";
      }
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };
  const set = (patch: Partial<DraftData>) => setData((d) => ({ ...d, ...patch }));

  return (
    <main data-testid="wizard">
      <div className="page-head">
        <div>
          <a href="#/commission" className="muted">
            ← Commission
          </a>
          <h1>
            {data.name || draft.instrument_id}{" "}
            <span className="badge sim" title="Version 1 commissions simulated sources only">
              Simulated
            </span>
          </h1>
          <p className="muted">
            <span className="mono">{draft.instrument_id}</span> · {draft.lab_id} ·{" "}
            {draft.state === "published"
              ? `published as ${draft.published_revision_id}`
              : draft.state === "abandoned"
                ? "abandoned draft"
                : draft.existing
                  ? `draft of a new revision (active r${draft.existing.revision})`
                  : "draft of a new instrument"}
          </p>
        </div>
      </div>
      <ol className="stepper" aria-label="Commissioning steps">
        {steps.map((name, i) => (
          <li key={name} aria-current={step === i + 1 ? "step" : undefined}>
            <button
              type="button"
              onClick={() => save(i + 1)}
              disabled={busy || (i + 1 === 6 && draft.state !== "published")}
            >
              <span className="num">{i + 1}</span> {name}
            </button>
          </li>
        ))}
      </ol>
      {error && (
        <p className="banner" role="alert">
          {error}
        </p>
      )}
      <section className="card card-pad" data-testid={`step-${step}`}>
        {step === 1 && (
          <StepIdentify
            data={data}
            set={set}
            editable={editable}
            collectors={collectors}
            custodians={custodians}
          />
        )}
        {step === 2 && (
          <StepConnect data={data} set={set} editable={editable} profiles={opts.profiles} />
        )}
        {step === 3 && <StepMap profile={profile} />}
        {step === 4 && (
          <StepPreview
            preview={preview}
            stored={draft.preview}
            run={() => act("preview")}
            busy={busy}
            editable={editable}
            ready={!!data.profile && data.seed !== undefined}
            tz={tz}
          />
        )}
        {step === 5 && (
          <StepApprove
            draft={draft}
            publish={() => act("publish")}
            abandon={() => act("abandon")}
            busy={busy}
          />
        )}
        {step === 6 && <StepVerify draft={draft} tz={tz} />}
        <div className="row wizard-nav">
          <button
            type="button"
            className="btn"
            disabled={busy || step === 1}
            onClick={() => save(step - 1)}
          >
            Back
          </button>
          <span className="spacer" />
          {editable && (
            <button type="button" className="btn" disabled={busy} onClick={() => save(step)}>
              Save draft
            </button>
          )}
          {step < 5 && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy}
              onClick={() => save(step + 1)}
            >
              Next: {steps[step]}
            </button>
          )}
        </div>
      </section>
    </main>
  );
}

function StepIdentify(props: {
  data: DraftData;
  set: (p: Partial<DraftData>) => void;
  editable: boolean;
  collectors: CommissioningOptions["collectors"];
  custodians: { username: string; display_name: string }[];
}) {
  const { data, set, editable, collectors, custodians } = props;
  const chosen = collectors.find((c) => c.id === data.collector_id);
  return (
    <>
      <h2 className="section-title">1 · Identify</h2>
      <fieldset disabled={!editable} className="form-grid">
        <label className="span2">
          Name
          <input
            required
            maxLength={80}
            value={data.name ?? ""}
            onChange={(e) => set({ name: e.target.value })}
          />
        </label>
        <label>
          Location
          <input
            maxLength={80}
            value={data.location ?? ""}
            onChange={(e) => set({ location: e.target.value || undefined })}
          />
        </label>
        <label>
          Asset id
          <input
            maxLength={64}
            value={data.asset_id ?? ""}
            onChange={(e) => set({ asset_id: e.target.value || undefined })}
          />
        </label>
        <label>
          Custodian
          <select
            value={data.custodian ?? ""}
            onChange={(e) => set({ custodian: e.target.value || undefined })}
          >
            <option value="">Choose…</option>
            {custodians.map((u) => (
              <option key={u.username} value={u.username}>
                {u.display_name} ({u.username})
              </option>
            ))}
          </select>
        </label>
        <label>
          Collector
          <select
            value={data.collector_id ?? ""}
            onChange={(e) => set({ collector_id: e.target.value || undefined })}
            aria-describedby="collector-check"
          >
            <option value="">Choose…</option>
            {collectors.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} ({c.id}) · {c.online ? "online" : "offline"}
              </option>
            ))}
          </select>
        </label>
        <label className="span2">
          Approved interface reference
          <input
            maxLength={200}
            value={data.interface_ref ?? ""}
            onChange={(e) => set({ interface_ref: e.target.value || undefined })}
            placeholder="document or ticket that approves this source"
          />
        </label>
        <label>
          Mode
          <select value="simulated" disabled>
            <option value="simulated">Simulated</option>
          </select>
        </label>
      </fieldset>
      <dl className="kv" id="collector-check" data-testid="collector-check">
        <dt>Collector check</dt>
        <dd>
          {!chosen ? (
            <span className="muted">choose a collector</span>
          ) : chosen.online ? (
            <span className="pill tone-ok">
              <span className="dot" /> online · heartbeat {chosen.heartbeat_age_s} s ago ·{" "}
              {chosen.software_version}
            </span>
          ) : (
            <span className="pill tone-warn">
              offline
              {chosen.last_heartbeat_at
                ? ` · last heartbeat ${Math.round(chosen.heartbeat_age_s ?? 0)} s ago`
                : " · never seen"}
            </span>
          )}
        </dd>
        <dt>Local driver</dt>
        <dd>Built-in simulator: no driver or device access needed.</dd>
        <dt>Real hardware</dt>
        <dd className="muted">
          Not available in version 1. Hardware adapters follow the instrument-specific approval
          (M4).
        </dd>
      </dl>
    </>
  );
}

function StepConnect(props: {
  data: DraftData;
  set: (p: Partial<DraftData>) => void;
  editable: boolean;
  profiles: ProfileSummary[];
}) {
  const { data, set, editable, profiles } = props;
  const p = profiles.find((x) => x.key === data.profile);
  return (
    <>
      <h2 className="section-title">2 · Connect</h2>
      <fieldset disabled={!editable} className="form-grid">
        <label>
          Simulator profile
          <select
            value={data.profile ?? ""}
            onChange={(e) => set({ profile: e.target.value || undefined })}
          >
            <option value="">Choose…</option>
            {profiles.map((x) => (
              <option key={x.key} value={x.key}>
                {x.title}
                {x.opt_in ? " (opt-in)" : ""}
              </option>
            ))}
          </select>
        </label>
        <label>
          Seed
          <input
            type="number"
            min={0}
            max={2147483647}
            step={1}
            value={data.seed ?? ""}
            onChange={(e) =>
              set({ seed: e.target.value === "" ? undefined : Math.trunc(Number(e.target.value)) })
            }
            aria-describedby="seed-help"
          />
          <span id="seed-help" className="faint">
            The same seed always produces the same readings (documented known answers).
          </span>
        </label>
      </fieldset>
      {p && (
        <dl className="kv">
          <dt>Adapter</dt>
          <dd className="mono">
            {p.adapter} v{p.adapter_version}
          </dd>
          <dt>Scenario</dt>
          <dd>The profile&apos;s documented program cycle, including its scripted faults.</dd>
          {p.cadence_note && (
            <>
              <dt>Cadence</dt>
              <dd>{p.cadence_note}</dd>
            </>
          )}
          {p.sign_convention && (
            <>
              <dt>Sign convention</dt>
              <dd>{p.sign_convention}</dd>
            </>
          )}
          <dt>Endpoint</dt>
          <dd className="muted">None: a simulator opens no device or network connection.</dd>
        </dl>
      )}
    </>
  );
}

function StepMap({ profile }: { profile: ProfileSummary | undefined }) {
  if (!profile) return <p className="muted">Choose a profile in step 2.</p>;
  return (
    <>
      <h2 className="section-title">3 · Map</h2>
      <p className="muted">
        Simulated profiles use their documented, read-only channel map. Editable addresses, scaling
        and byte order belong to future hardware adapters.
      </p>
      <div className="table-wrap">
        <table data-testid="channel-map">
          <thead>
            <tr>
              <th>Channel</th>
              <th>Quantity</th>
              <th>Unit</th>
              <th>Type</th>
              <th>Role</th>
              <th>Cadence</th>
              <th>Valid range</th>
            </tr>
          </thead>
          <tbody>
            {profile.channels.map((c) => (
              <tr key={c.key}>
                <td>
                  {c.name} <span className="mono faint">{c.key}</span>
                </td>
                <td>{c.quantity}</td>
                <td>{c.unit || "—"}</td>
                <td>
                  {c.data_type}
                  {c.enum_labels && (
                    <span className="faint"> ({Object.values(c.enum_labels).join(", ")})</span>
                  )}
                </td>
                <td>{c.role}</td>
                <td className="num">{c.expected_cadence_s} s</td>
                <td className="num">
                  {c.valid_min !== null && c.valid_max !== null
                    ? `${c.valid_min} … ${c.valid_max}`
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="faint">Source timestamps: {profile.source_time_meaning}.</p>
    </>
  );
}

function MiniTrend({ points, label }: { points: [string, number | null][]; label: string }) {
  const vals = points.map((p) => p[1]).filter((v): v is number => v !== null);
  if (vals.length < 2) return null;
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const span = hi - lo || 1;
  const d = points
    .map((p, i) =>
      p[1] === null ? null : `${(i / (points.length - 1)) * 200},${38 - ((p[1] - lo) / span) * 34}`,
    )
    .filter(Boolean)
    .join(" ");
  return (
    <svg className="mini-trend" viewBox="0 0 200 40" role="img" aria-label={label}>
      <polyline points={d} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

function StepPreview(props: {
  preview: PreviewResult | null;
  stored: DraftView["preview"];
  run: () => void;
  busy: boolean;
  editable: boolean;
  ready: boolean;
  tz: string;
}) {
  const { preview, stored, run, busy, editable, ready, tz } = props;
  const keys = preview ? preview.channels.map((c) => c.key) : [];
  return (
    <>
      <h2 className="section-title">4 · Preview</h2>
      <p className="muted">
        Runs the same deterministic simulator the collector will run, applies the collector&apos;s
        quality rules, then a scripted dropout to check recovery. Nothing is stored as telemetry.
      </p>
      {editable && (
        <button
          type="button"
          className="btn btn-primary"
          onClick={run}
          disabled={busy || !ready}
          data-testid="run-preview"
        >
          {busy ? "Running…" : preview || stored ? "Run preview again" : "Run preview"}
        </button>
      )}
      {!ready && <p className="notice">Choose a profile and seed in step 2 first.</p>}
      {!preview && stored && (
        <p className={stored.stale ? "notice" : "muted"}>
          Last preview {formatTime(stored.at, tz)}: {stored.ok ? "passed" : "failed"}
          {stored.stale && " — the profile or seed changed since; run it again."}
        </p>
      )}
      {preview && (
        <>
          <p>
            <span
              className={`pill ${preview.ok ? "tone-ok" : "tone-bad"}`}
              data-testid="preview-result"
            >
              {preview.ok ? "Preview passed" : "Preview failed"}
            </span>{" "}
            <span className="muted">
              {preview.window.frames} frames from {formatTime(preview.window.start, tz)} · simulated
            </span>
          </p>
          <CheckList checks={preview.checks} />
          <div className="tiles">
            {Object.entries(preview.trend).map(([k, pts]) => {
              const ch = preview.channels.find((c) => c.key === k);
              return (
                <div className="tile" key={k}>
                  <span className="tile-name">
                    {ch?.name} ({ch?.unit || "—"})
                  </span>
                  <MiniTrend points={pts} label={`${ch?.name} preview trend`} />
                </div>
              );
            })}
          </div>
          <div className="table-wrap">
            <table data-testid="preview-frames">
              <thead>
                <tr>
                  <th>Capture time</th>
                  {keys.map((k) => (
                    <th key={k}>{k}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {preview.frames.map((f) => (
                  <tr key={f.t}>
                    <td className="num">{formatTime(f.t, tz)}</td>
                    {keys.map((k) => {
                      const c = f.channels?.[k];
                      return (
                        <td key={k} className="num" title={c ? `raw ${c.raw ?? "—"}` : undefined}>
                          {c ? (
                            <>
                              {String(c.value ?? "—")} {c.unit}
                              {c.quality !== "good" && (
                                <span className="pill tone-bad"> {c.reason ?? c.quality}</span>
                              )}
                            </>
                          ) : (
                            <span className="muted">{f.unavailable ?? "—"}</span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="faint">
            Dropout drill: source silent {formatTime(preview.drill.from, tz)} –{" "}
            {formatTime(preview.drill.to, tz)} ({preview.drill.unavailable_frames} frames), then
            readings resume.
          </p>
        </>
      )}
    </>
  );
}

function show(v: unknown): string {
  return v === null || v === undefined || v === "" ? "—" : String(v);
}

function StepApprove(props: {
  draft: DraftView;
  publish: () => void;
  abandon: () => void;
  busy: boolean;
}) {
  const { draft, publish, abandon, busy } = props;
  const pv = draft.preview;
  const canPublish = draft.state === "draft" && draft.missing.length === 0 && !!pv?.ok && !pv.stale;
  return (
    <>
      <h2 className="section-title">5 · Approve</h2>
      {draft.state !== "draft" && (
        <p className="notice">
          This draft is {draft.state}
          {draft.published_revision_id && ` as ${draft.published_revision_id}`}.
        </p>
      )}
      <h3>Compared with the active revision</h3>
      {draft.changes.length === 0 ? (
        <p className="muted">No change against the active revision.</p>
      ) : (
        <div className="table-wrap">
          <table data-testid="changes">
            <thead>
              <tr>
                <th>Field</th>
                <th>Active{draft.existing ? ` (r${draft.existing.revision})` : ""}</th>
                <th>Draft</th>
              </tr>
            </thead>
            <tbody>
              {draft.changes.map((c) => (
                <tr key={c.field}>
                  <td>{c.field}</td>
                  <td>{show(c.before)}</td>
                  <td>
                    <strong>{show(c.after)}</strong>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <ul className="checks">
        <li className={draft.missing.length ? "tone-bad" : "tone-ok"}>
          {draft.missing.length ? `✕ Missing: ${draft.missing.join(", ")}` : "✓ Draft complete"}
        </li>
        <li className={pv?.ok && !pv.stale ? "tone-ok" : "tone-bad"}>
          {pv?.ok && !pv.stale
            ? "✓ Passing preview of this profile and seed"
            : "✕ Run a passing preview of this profile and seed (step 4)"}
        </li>
        <li className="tone-ok">✓ Read-only: the simulator accepts no commands</li>
      </ul>
      <p className="muted">
        Publishing writes an immutable profile revision. The active revision stays in service until
        the collector reports the new one.
      </p>
      {draft.state === "draft" && (
        <div className="row">
          <button
            type="button"
            className="btn btn-primary"
            onClick={publish}
            disabled={busy || !canPublish}
            data-testid="publish"
          >
            Publish revision
          </button>
          <button type="button" className="btn btn-ghost" onClick={abandon} disabled={busy}>
            Abandon draft
          </button>
        </div>
      )}
    </>
  );
}

const VERIFY_TEXT: Record<VerifyResult["state"], [string, string]> = {
  awaiting_collector: [
    "Waiting for the collector",
    "An administrator downloads the collector configuration (Collectors page), installs it on the collector PC and restarts the collector. Until it reports this revision, the previous one stays active.",
  ],
  awaiting_samples: [
    "Revision applied, waiting for samples",
    "The collector reports this revision. The first committed samples complete activation.",
  ],
  receiving: [
    "Active: receiving samples",
    "Close the browser and come back later: history continues, because acquisition does not depend on this page.",
  ],
};

function StepVerify({ draft, tz }: { draft: DraftView; tz: string }) {
  const v = usePolling<VerifyResult>(
    draft.state === "published"
      ? (s) => api.get(`/api/v1/commissioning/drafts/${draft.id}/verify`, s)
      : null,
    5000,
    `verify:${draft.id}`,
  );
  const downloadRecord = async () => {
    const rec = await api.get<unknown>(`/api/v1/commissioning/drafts/${draft.id}/record`);
    const blob = new Blob([JSON.stringify(rec, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `commissioning-${draft.instrument_id}-${draft.published_revision_id ?? draft.id}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  if (draft.state !== "published") return <p className="muted">Publish the draft first.</p>;
  const r = v.data;
  return (
    <>
      <h2 className="section-title">6 · Verify</h2>
      {!r ? (
        <p className="muted">Checking…</p>
      ) : (
        <>
          <p>
            <span
              className={`pill ${r.state === "receiving" ? "tone-ok" : "tone-warn"}`}
              data-testid="verify-state"
            >
              {VERIFY_TEXT[r.state][0]}
            </span>
          </p>
          <p className="muted">{VERIFY_TEXT[r.state][1]}</p>
          <dl className="kv">
            <dt>Revision</dt>
            <dd className="mono">{r.revision_id}</dd>
            <dt>Collector</dt>
            <dd>
              {r.collector.id} · {r.collector.online ? "online" : "offline"} · applied{" "}
              <span className="mono">{r.collector.applied_revision_id ?? "—"}</span>
            </dd>
            <dt>Committed samples</dt>
            <dd className="num">
              {r.samples.count}
              {r.samples.first_received && ` since ${formatTime(r.samples.first_received, tz)}`}
            </dd>
            {r.latest.map((l) => (
              <div key={l.channel_id} style={{ display: "contents" }}>
                <dt className="mono">{l.channel_id}</dt>
                <dd className="num">
                  {String(l.value)} · {l.quality} · {formatTime(l.event_time, tz)}
                </dd>
              </div>
            ))}
          </dl>
          <div className="row">
            <a className="btn" href={`#/instrument/${encodeURIComponent(draft.instrument_id)}`}>
              Open history
            </a>
            <button type="button" className="btn" onClick={() => window.print()}>
              Print record
            </button>
            <button type="button" className="btn" onClick={downloadRecord} data-testid="record">
              Download commissioning record
            </button>
          </div>
          <p className="faint">Evidence from simulated sources. It is not field acceptance.</p>
        </>
      )}
    </>
  );
}
