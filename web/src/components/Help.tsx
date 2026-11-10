import { ACQ_LABEL, COLLECTOR_LABEL } from "../status";

/** Local help: shipped with the release, no external links needed on the lab network. */
export function Help() {
  return (
    <main className="help" data-testid="help">
      <div className="page-head">
        <div>
          <h1>Help</h1>
          <p className="sub">
            How to read the monitor. This installation shows simulated instruments only.
          </p>
        </div>
      </div>

      <section className="card card-pad">
        <h2>Status words</h2>
        <p>Three things are reported separately, because one can fail without the others:</p>
        <table>
          <tbody>
            {Object.entries(ACQ_LABEL).map(([k, l]) => (
              <tr key={k}>
                <td>
                  <span className={`pill tone-${l.tone}`}>
                    <span aria-hidden="true">{l.icon}</span> {l.text}
                  </span>
                </td>
                <td>{ACQ_TEXT[k] ?? ""}</td>
              </tr>
            ))}
            {Object.entries(COLLECTOR_LABEL).map(([k, l]) => (
              <tr key={k}>
                <td>
                  <span className={`pill tone-${l.tone}`}>
                    <span aria-hidden="true">{l.icon}</span> {l.text}
                  </span>
                </td>
                <td>{COLLECTOR_TEXT[k] ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="sub">
          <strong>Process state</strong> (for example Soak or Testing) comes only from the
          instrument&apos;s own state channel. When that channel is not fresh the state shows as
          Unknown with the last known value — the monitor never guesses it.
        </p>
      </section>

      <section className="card card-pad">
        <h2>Reading the charts</h2>
        <ul>
          <li>
            <strong>Grey shaded bands</strong> are gaps: no reading arrived for longer than the
            channel&apos;s freshness window (at least 5 s). The line is broken there; nothing is
            interpolated.
          </li>
          <li>
            <strong>Red dashed lines</strong> mark bad readings (for example an open thermocouple).
            Their value is withheld from statistics; the reason is in the tooltip and the readings
            table.
          </li>
          <li>
            <strong>Long ranges</strong> are aggregated: the shaded band is the minimum–maximum of
            each bucket and the line is the mean of valid readings. A single extreme reading still
            shows in the band; the summary names its exact time. Zoom in to see raw samples.
          </li>
          <li>
            <strong>Live</strong> follows the newest data. Zooming, panning or choosing a custom
            range <strong>pauses</strong> the view; use Resume live to follow again.
          </li>
          <li>
            Times are shown in IST by default; switch to UTC in the top bar. Exports always use UTC
            (RFC 3339).
          </li>
        </ul>
      </section>

      <section className="card card-pad">
        <h2>Runs, annotations and analysis</h2>
        <ul>
          <li>
            Runs reported by an instrument program (a tensile test, a heat treatment) appear
            automatically. Scientists can also record their own runs; these are records only and
            never start or stop an instrument.
          </li>
          <li>
            Corrections require a reason and are kept in the audit log with the previous values.
            Annotations can be corrected or retracted, never silently deleted.
          </li>
          <li>
            Tensile: engineering stress = force / original area, strain = extension / original gauge
            length; UTS, Rp0.2 and the modulus are reported only for complete runs. DSC: heat flow
            is shown <em>exo up</em>; peak areas are estimates with a straight baseline.
          </li>
          <li>
            Furnace: time within tolerance compares the process value with the recorded setpoint
            during the Soak phase; gaps and bad readings count as unobserved.
          </li>
        </ul>
      </section>

      <section className="card card-pad">
        <h2>Exports</h2>
        <p>
          <strong>CSV (raw rows)</strong> downloads the displayed range directly.{" "}
          <strong>Export displayed range</strong> creates a ZIP with the raw rows and a manifest
          (range, time zone, instrument profile revisions, database snapshot, counts and checksums),
          kept for 7 days. Simulated data is labelled in file names and every row.
        </p>
      </section>

      <section className="card card-pad">
        <h2>Commissioning an instrument</h2>
        <p>
          Custodians and administrators open <strong>Commission</strong>. The wizard saves a draft
          on the server after every step, so you can stop and resume from the list of open drafts.
        </p>
        <ol>
          <li>
            <strong>Identify:</strong> name, location, asset id, custodian and collector. The
            collector check shows whether it sent a heartbeat in the last 30 s.
          </li>
          <li>
            <strong>Connect:</strong> choose the simulator profile and seed. The same seed always
            gives the same readings.
          </li>
          <li>
            <strong>Map:</strong> review the documented channels, units, cadence and valid ranges.
          </li>
          <li>
            <strong>Preview:</strong> runs the simulator with the collector&apos;s quality rules and
            a scripted dropout. Publishing needs a passing preview of the chosen profile and seed.
          </li>
          <li>
            <strong>Approve:</strong> compare with the active revision and publish. Publishing
            writes a new, unchangeable profile revision.
          </li>
          <li>
            <strong>Verify:</strong> an administrator downloads the collector configuration from{" "}
            <strong>Collectors</strong> and restarts the collector. The instrument shows as active
            only after the collector reports the new revision and its first samples are stored.
            Until then the previous revision stays in service. Print or download the commissioning
            record.
          </li>
        </ol>
      </section>

      <section className="card card-pad">
        <h2>Roles</h2>
        <p>
          Viewers read data and keep personal views. Scientists also record runs and annotations.
          Custodians also share views as lab templates and commission instruments. Administrators
          can do all of that and also add collectors, issue collector credentials and revoke
          collectors. Accounts are managed by the site administrator.
        </p>
      </section>
    </main>
  );
}

const ACQ_TEXT: Record<string, string> = {
  receiving: "Fresh readings from the current collector on every channel.",
  degraded: "Readings arrive but some are bad or some channels are not fresh.",
  stale: "No fresh reading. The reason says whether the collector is online, offline or delayed.",
  source_unavailable: "The collector is running but the instrument does not answer.",
  waiting: "No reading has arrived yet (new instrument, or no collector assigned).",
  paused: "Acquisition was paused deliberately.",
};

const COLLECTOR_TEXT: Record<string, string> = {
  online: "Heartbeat received within the last 30 s.",
  offline:
    "No heartbeat for more than 30 s. Readings may still be stored on the instrument PC and uploaded later.",
  never_seen: "The collector has never connected.",
  revoked: "The collector's credential was revoked by an administrator.",
};
