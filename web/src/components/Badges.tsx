import { ACQ_LABEL, COLLECTOR_LABEL } from "../status";
import type { AcquisitionState } from "../types";

export function SimBadge({ simulated }: { simulated: boolean }) {
  if (!simulated) return null;
  return (
    <span
      className="badge sim"
      title="Synthetic data from the simulator, not a real instrument"
    >
      ◇ SIMULATED
    </span>
  );
}

export function AcqBadge({ state }: { state: AcquisitionState }) {
  const l = ACQ_LABEL[state] ?? { text: state, icon: "?", tone: "muted" };
  return (
    <span className={`pill tone-${l.tone}`} data-testid="acq-state">
      <span aria-hidden="true">{l.icon}</span> {l.text}
    </span>
  );
}

export function CollectorBadge({ state }: { state: string }) {
  const l = COLLECTOR_LABEL[state] ?? { text: state, icon: "?", tone: "muted" };
  return (
    <span className={`pill tone-${l.tone}`} data-testid="collector-state">
      <span aria-hidden="true">{l.icon}</span> {l.text}
    </span>
  );
}

export function RunBadge({ active }: { active: boolean }) {
  return active ? (
    <span className="pill tone-ok">
      <span className="dot" aria-hidden="true" /> Running
    </span>
  ) : (
    <span className="pill tone-muted">Complete</span>
  );
}
