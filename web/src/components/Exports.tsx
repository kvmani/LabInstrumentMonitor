import { useState } from "react";
import { api, ApiError, qs } from "../api";
import { usePolling } from "../hooks";
import { formatTime, isoUtc } from "../time";
import type { ExportJobView } from "../types";

const STATUS_TONE: Record<ExportJobView["status"], string> = {
  queued: "tone-muted",
  running: "tone-warn",
  succeeded: "tone-ok",
  failed: "tone-bad",
  expired: "tone-muted",
};

function size(bytes: number | null): string {
  if (bytes === null) return "";
  return bytes < 1024 * 1024
    ? `${(bytes / 1024).toFixed(1)} KiB`
    : `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
}

/** Durable exports: a ZIP with raw rows and a manifest, produced by the background worker. */
export function ExportsPanel({
  instrumentId,
  range,
  tz,
  tzLabel,
}: {
  instrumentId: string;
  range: { start: number; end: number } | null;
  tz: string;
  tzLabel: string;
}) {
  const [error, setError] = useState<string | null>(null);
  const jobs = usePolling<{ items: ExportJobView[] }>(
    (signal) =>
      api.get(`/api/v1/exports?${qs({ instrument_id: instrumentId })}`, signal),
    3000,
    `exports:${instrumentId}`,
  );
  const request = async () => {
    if (!range) return;
    setError(null);
    try {
      await api.post(
        `/api/v1/instruments/${encodeURIComponent(instrumentId)}/exports`,
        { start: isoUtc(range.start), end: isoUtc(range.end) },
      );
      jobs.refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "request failed");
    }
  };
  const items = jobs.data?.items ?? [];
  return (
    <section
      className="card card-pad"
      aria-labelledby="exp-title"
      data-testid="exports"
    >
      <div className="toolbar">
        <h2 id="exp-title">Exports</h2>
        <span className="sub">
          ZIP with raw rows (CSV) and a manifest: range, time zone, profile
          revisions, snapshot cutoff, counts, checksums
        </span>
        <span className="spacer" />
        <button
          type="button"
          className="btn-primary"
          onClick={request}
          disabled={!range}
          data-testid="request-export"
        >
          ⤓ Export displayed range
        </button>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {items.length === 0 ? (
        <p className="sub">No exports yet. Exports are kept for 7 days.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Range ({tzLabel})</th>
                <th scope="col">Status</th>
                <th scope="col">Rows</th>
                <th scope="col">SHA-256</th>
                <th scope="col">
                  <span className="sr-only">Download</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {items.slice(0, 8).map((j) => (
                <tr key={j.id}>
                  <td className="mono sub">
                    {formatTime(j.params.start, tz)} –{" "}
                    {formatTime(j.params.end, tz)}
                  </td>
                  <td>
                    <span className={`pill ${STATUS_TONE[j.status]}`}>
                      {j.status}
                    </span>
                    {j.error && <div className="sub error">{j.error}</div>}
                  </td>
                  <td className="num">
                    {(j.row_count ?? j.params.estimated_rows).toLocaleString()}
                  </td>
                  <td className="mono sub" title={j.sha256 ?? ""}>
                    {j.sha256 ? `${j.sha256.slice(0, 12)}…` : "—"}{" "}
                    {size(j.byte_size)}
                  </td>
                  <td>
                    {j.status === "succeeded" && (
                      <a
                        className="btn"
                        href={`/api/v1/exports/${j.id}/download`}
                        download
                        data-testid="export-download"
                      >
                        Download
                      </a>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
