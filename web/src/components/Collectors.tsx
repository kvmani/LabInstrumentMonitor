import { useState } from "react";
import { api, ApiError } from "../api";
import { usePolling } from "../hooks";
import { formatTime } from "../time";
import type { CollectorView } from "../types";

function message(e: unknown): string {
  return e instanceof ApiError ? e.message : "request failed";
}

/**
 * Collector provisioning (site administrator). Custodians see the list read-only. A package holds
 * collector.toml for the current assignments and, only when asked, a new credential that revokes
 * the previous ones. The credential exists only in that download.
 */
export function Collectors({ labs, tz }: { labs: Record<string, string>; tz: string }) {
  const list = usePolling<{ items: CollectorView[] }>(
    (s) => api.get("/api/v1/collectors", s),
    10000,
    "collectors",
  );
  const adminLabs = Object.entries(labs)
    .filter(([, role]) => role === "administrator")
    .map(([lab]) => lab);
  const [serverUrl, setServerUrl] = useState(window.location.origin);
  const [form, setForm] = useState({ collector_id: "", lab_id: adminLabs[0] ?? "", name: "" });
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const run = async (fn: () => Promise<string | void>) => {
    setError(null);
    setNotice(null);
    try {
      const msg = await fn();
      if (msg) setNotice(msg);
      list.refresh();
    } catch (e) {
      setError(message(e));
    }
  };
  const add = (e: React.FormEvent) => {
    e.preventDefault();
    void run(async () => {
      await api.post("/api/v1/collectors", form);
      setForm({ ...form, collector_id: "", name: "" });
      return `Collector ${form.collector_id} added. Commission an instrument on it, then download its package.`;
    });
  };
  const pkg = (c: CollectorView, issueToken: boolean) =>
    run(async () => {
      if (
        issueToken &&
        !window.confirm(
          `Issue a new credential for ${c.id}? Its current credential stops working at once; the collector keeps spooling until the new one is installed.`,
        )
      )
        return;
      const name = await api.download(`/api/v1/collectors/${encodeURIComponent(c.id)}/package`, {
        server_url: serverUrl,
        issue_token: issueToken,
      });
      return issueToken
        ? `${name} downloaded. It holds the only copy of the new credential: transfer it through the approved channel, restrict the token file, restart the collector, then delete the archive.`
        : `${name} downloaded (configuration only; the installed credential stays valid).`;
    });
  const revoke = (c: CollectorView) =>
    run(async () => {
      const reason = window.prompt(
        `Revoke collector ${c.id}? Uploads stop at once. Run "labmon-collector flush" first if the PC is being retired. Reason:`,
      );
      if (!reason) return;
      await api.post(`/api/v1/collectors/${encodeURIComponent(c.id)}/revoke`, { reason });
      return `Collector ${c.id} revoked.`;
    });

  const items = list.data?.items ?? [];
  return (
    <main data-testid="collectors">
      <div className="page-head">
        <div>
          <h1>Collectors</h1>
          <p className="muted">
            Collectors acquire and spool readings on instrument PCs. Online means a heartbeat in the
            last 30 s.
          </p>
        </div>
        <a className="btn" href="#/commission">
          Commission
        </a>
      </div>
      {error && (
        <p className="banner" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="notice" role="status" data-testid="collector-notice">
          {notice}
        </p>
      )}
      <section className="card card-pad">
        <div className="table-wrap">
          <table data-testid="collector-table">
            <thead>
              <tr>
                <th>Collector</th>
                <th>Lab</th>
                <th>State</th>
                <th>Last heartbeat</th>
                <th>Instruments</th>
                <th>Credentials</th>
                {adminLabs.length > 0 && <th>Actions</th>}
              </tr>
            </thead>
            <tbody>
              {items.map((c) => (
                <tr key={c.id} data-testid={`collector-${c.id}`}>
                  <td>
                    {c.name} <span className="mono faint">{c.id}</span>
                  </td>
                  <td>{c.lab_id}</td>
                  <td>
                    {c.revoked ? (
                      <span className="pill tone-muted">revoked</span>
                    ) : c.online ? (
                      <span className="pill tone-ok">
                        <span className="dot" /> online
                      </span>
                    ) : (
                      <span className="pill tone-warn">offline</span>
                    )}
                    {c.upload_fault && <div className="error">{c.upload_fault}</div>}
                  </td>
                  <td className="num">
                    {c.last_heartbeat_at ? formatTime(c.last_heartbeat_at, tz) : "never"}
                    {c.software_version && <div className="faint">v{c.software_version}</div>}
                  </td>
                  <td className="mono">{c.assigned_instruments.join(", ") || "—"}</td>
                  <td className="num">{c.active_credentials}</td>
                  {adminLabs.length > 0 && (
                    <td>
                      {adminLabs.includes(c.lab_id) && !c.revoked && (
                        <div className="row">
                          <button type="button" className="btn" onClick={() => pkg(c, false)}>
                            Configuration
                          </button>
                          <button type="button" className="btn" onClick={() => pkg(c, true)}>
                            New credential
                          </button>
                          <button type="button" className="btn btn-ghost" onClick={() => revoke(c)}>
                            Revoke
                          </button>
                        </div>
                      )}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {items.length === 0 && <p className="muted">No collectors in your labs.</p>}
      </section>
      {adminLabs.length > 0 && (
        <div className="split" style={{ marginTop: 16 }}>
          <form className="card card-pad" onSubmit={add} aria-label="Add collector">
            <h2 className="section-title">Add collector</h2>
            <div className="form-grid">
              <label>
                Collector id
                <input
                  required
                  pattern="[a-z0-9][a-z0-9._-]{0,63}"
                  value={form.collector_id}
                  onChange={(e) => setForm({ ...form, collector_id: e.target.value.toLowerCase() })}
                />
              </label>
              <label>
                Lab
                <select
                  value={form.lab_id}
                  onChange={(e) => setForm({ ...form, lab_id: e.target.value })}
                >
                  {adminLabs.map((l) => (
                    <option key={l}>{l}</option>
                  ))}
                </select>
              </label>
              <label className="span2">
                Name
                <input
                  required
                  maxLength={80}
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="e.g. Metals bay PC"
                />
              </label>
            </div>
            <button className="btn btn-primary" type="submit">
              Add collector
            </button>
          </form>
          <section className="card card-pad">
            <h2 className="section-title">Package settings</h2>
            <label className="form-grid">
              <span className="span2">
                Monitor URL the collector uploads to
                <input
                  value={serverUrl}
                  onChange={(e) => setServerUrl(e.target.value)}
                  aria-describedby="url-help"
                  style={{ width: "100%" }}
                />
              </span>
            </label>
            <p id="url-help" className="faint">
              scheme://host[:port] only. Production requires HTTPS with the site certificate.
            </p>
          </section>
        </div>
      )}
    </main>
  );
}
