import { formatAge, formatTime } from "../time";

/** Prominent page-level status: browser connectivity and freshness of what is on screen. */
export function ConnectionBanner(props: {
  online: boolean;
  failing: boolean;
  lastSuccessAt: number | null;
  now: number;
  tz: string;
  tzLabel: string;
}) {
  const { online, failing, lastSuccessAt, now, tz, tzLabel } = props;
  if (online && !failing) return null;
  const shown = lastSuccessAt
    ? `showing data last refreshed at ${formatTime(lastSuccessAt, tz, false)} ${tzLabel} (${formatAge((now - lastSuccessAt) / 1000)})`
    : "no data loaded yet";
  return (
    <div className="banner" role="alert" data-testid="connection-banner">
      <strong>
        {online
          ? "Cannot reach the monitor service"
          : "This browser is offline"}
      </strong>{" "}
      · {shown}. Acquisition continues on the collector; values below may be out
      of date.
    </div>
  );
}
