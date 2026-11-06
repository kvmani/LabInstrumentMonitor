import { useState } from "react";
import { api, ApiError } from "../api";
import { formatTime, fromLocalInput, isoUtc, toLocalInput } from "../time";
import type { AnnotationView, RunView } from "../types";

export const RECORD_ROLES = new Set(["scientist", "custodian", "administrator"]);

function errorText(e: unknown): string {
  return e instanceof ApiError ? e.message : "Cannot reach the monitor service";
}

/** Converts a datetime-local value in the display zone to UTC ISO, or null when empty. */
function toIso(value: string, tz: string): string | null {
  if (!value) return null;
  const ms = fromLocalInput(value, tz);
  return Number.isFinite(ms) ? isoUtc(ms) : null;
}

// --- annotations ----------------------------------------------------------------------------

export function AnnotationPanel(props: {
  instrumentId: string;
  items: AnnotationView[];
  canRecord: boolean;
  tz: string;
  tzLabel: string;
  onChanged: () => void;
}) {
  const { instrumentId, items, canRecord, tz, tzLabel, onChanged } = props;
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ at: toLocalInput(Date.now(), tz), end: "", text: "" });
  const [editing, setEditing] = useState<{ id: number; text: string; reason: string; mode: "edit" | "retract" } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChanged();
      return true;
    } catch (e) {
      setError(errorText(e));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    const ok = await run(() =>
      api.post(`/api/v1/instruments/${encodeURIComponent(instrumentId)}/annotations`, {
        at: toIso(form.at, tz),
        end_at: toIso(form.end, tz),
        text: form.text,
      }),
    );
    if (ok) {
      setAdding(false);
      setForm({ at: toLocalInput(Date.now(), tz), end: "", text: "" });
    }
  };

  const saveEdit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!editing) return;
    const ok = await run(() =>
      editing.mode === "edit"
        ? api.patch(`/api/v1/annotations/${editing.id}`, { text: editing.text, reason: editing.reason })
        : api.post(`/api/v1/annotations/${editing.id}/retract`, { reason: editing.reason }),
    );
    if (ok) setEditing(null);
  };

  return (
    <section className="card card-pad" aria-labelledby="ann-title" data-testid="annotations">
      <div className="toolbar">
        <h2 id="ann-title">Annotations in this range</h2>
        <span className="sub">{items.length} shown · times in {tzLabel}</span>
        <span className="spacer" />
        {canRecord ? (
          <button type="button" className="btn-primary" onClick={() => setAdding(!adding)} aria-expanded={adding}>
            ✎ Add annotation
          </button>
        ) : (
          <span className="sub">Read-only role: annotations are view-only</span>
        )}
      </div>
      {adding && (
        <form className="toolbar" onSubmit={add} aria-label="New annotation">
          <label>
            At ({tzLabel}) <input type="datetime-local" required value={form.at} onChange={(e) => setForm({ ...form, at: e.target.value })} />
          </label>
          <label>
            Until (optional) <input type="datetime-local" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} />
          </label>
          <label style={{ flex: 1, minWidth: 220 }}>
            <span className="sr-only">Annotation text</span>
            <input style={{ width: "100%" }} required maxLength={1000} placeholder="What happened? e.g. door opened, sample changed" value={form.text} onChange={(e) => setForm({ ...form, text: e.target.value })} />
          </label>
          <button type="submit" className="btn-primary" disabled={busy}>
            Save
          </button>
        </form>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {items.length === 0 ? (
        <p className="sub">No annotations in the displayed range.</p>
      ) : (
        <ul className="ann-list">
          {items.map((a) => (
            <li key={a.id}>
              <span className="mono sub">
                {formatTime(a.at, tz)}
                {a.end_at ? ` – ${formatTime(a.end_at, tz, false)}` : ""}
              </span>{" "}
              <strong>{a.text}</strong>{" "}
              <span className="sub">
                · {a.author ?? "unknown"}
                {a.revision > 1 ? ` · corrected (rev ${a.revision})` : ""}
              </span>
              {canRecord && (
                <span className="row" style={{ display: "inline-flex", marginLeft: 8 }}>
                  <button type="button" className="btn-ghost" onClick={() => setEditing({ id: a.id, text: a.text, reason: "", mode: "edit" })}>
                    Correct
                  </button>
                  <button type="button" className="btn-ghost" onClick={() => setEditing({ id: a.id, text: a.text, reason: "", mode: "retract" })}>
                    Retract
                  </button>
                </span>
              )}
              {editing?.id === a.id && (
                <form className="toolbar" onSubmit={saveEdit} aria-label={editing.mode === "edit" ? "Correct annotation" : "Retract annotation"}>
                  {editing.mode === "edit" && (
                    <input style={{ flex: 1, minWidth: 220 }} required maxLength={1000} aria-label="Corrected text" value={editing.text} onChange={(e) => setEditing({ ...editing, text: e.target.value })} />
                  )}
                  <input style={{ flex: 1, minWidth: 200 }} required minLength={3} maxLength={500} aria-label="Reason (recorded in the audit log)" placeholder="Reason (recorded in the audit log)" value={editing.reason} onChange={(e) => setEditing({ ...editing, reason: e.target.value })} />
                  <button type="submit" className="btn-primary" disabled={busy}>
                    {editing.mode === "edit" ? "Save correction" : "Retract"}
                  </button>
                  <button type="button" onClick={() => setEditing(null)}>
                    Cancel
                  </button>
                </form>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// --- runs -----------------------------------------------------------------------------------

export function NewRunForm({ instrumentId, tz, tzLabel, onCreated, onCancel }: { instrumentId: string; tz: string; tzLabel: string; onCreated: (r: RunView) => void; onCancel: () => void }) {
  const [form, setForm] = useState({ name: "", start: toLocalInput(Date.now() - 3600_000, tz), end: "", notes: "", tags: "" });
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const run = await api.post<RunView>(`/api/v1/instruments/${encodeURIComponent(instrumentId)}/runs`, {
        name: form.name,
        started_at: toIso(form.start, tz),
        ended_at: toIso(form.end, tz),
        notes: form.notes || null,
        tags: form.tags.split(",").map((t) => t.trim()).filter(Boolean),
      });
      onCreated(run);
    } catch (err) {
      setError(errorText(err));
    }
  };
  return (
    <form className="card card-pad form-grid" onSubmit={submit} aria-label="New run" data-testid="new-run-form">
      <h2>New run record</h2>
      <p className="sub">A run is a record of an experiment window. It does not start or stop the instrument.</p>
      <label>
        Name <input required maxLength={120} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
      </label>
      <label>
        Start ({tzLabel}) <input type="datetime-local" required value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} />
      </label>
      <label>
        End (leave empty while running) <input type="datetime-local" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} />
      </label>
      <label>
        Tags (comma separated) <input value={form.tags} onChange={(e) => setForm({ ...form, tags: e.target.value })} placeholder="anneal, batch-7" />
      </label>
      <label className="span2">
        Notes <textarea rows={3} maxLength={4000} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
      </label>
      {error && (
        <p className="error span2" role="alert">
          {error}
        </p>
      )}
      <div className="row span2">
        <button type="submit" className="btn-primary">
          Create run
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function RunEditForm({ run, tz, tzLabel, onSaved, onCancel }: { run: RunView; tz: string; tzLabel: string; onSaved: () => void; onCancel: () => void }) {
  const user = run.source === "user";
  const [form, setForm] = useState({
    name: run.name,
    end: run.ended_at ? toLocalInput(Date.parse(run.ended_at), tz) : "",
    notes: run.notes ?? "",
    tags: run.tags.join(", "),
    reason: "",
  });
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const body: Record<string, unknown> = { reason: form.reason, notes: form.notes, tags: form.tags.split(",").map((t) => t.trim()).filter(Boolean) };
    if (user) {
      if (form.name !== run.name) body.name = form.name;
      const end = toIso(form.end, tz);
      if (end && end !== run.ended_at) body.ended_at = end;
    }
    try {
      await api.patch(`/api/v1/runs/${encodeURIComponent(run.id)}`, body);
      onSaved();
    } catch (err) {
      setError(errorText(err));
    }
  };
  return (
    <form className="card card-pad form-grid" onSubmit={submit} aria-label="Correct run record" data-testid="edit-run-form">
      <h3 className="span2">Correct run record</h3>
      {user ? (
        <>
          <label>
            Name <input required maxLength={120} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </label>
          <label>
            End ({tzLabel}) <input type="datetime-local" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} />
          </label>
        </>
      ) : (
        <p className="sub span2">Name and window come from the instrument; you can add notes and tags.</p>
      )}
      <label>
        Tags <input value={form.tags} onChange={(e) => setForm({ ...form, tags: e.target.value })} />
      </label>
      <label>
        Reason (recorded in the audit log) <input required minLength={3} maxLength={500} value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })} />
      </label>
      <label className="span2">
        Notes <textarea rows={3} maxLength={4000} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
      </label>
      {error && (
        <p className="error span2" role="alert">
          {error}
        </p>
      )}
      <div className="row span2">
        <button type="submit" className="btn-primary">
          Save correction
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
