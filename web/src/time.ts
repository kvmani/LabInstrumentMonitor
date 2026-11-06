export type Zone = "local" | "UTC";

export const DEFAULT_ZONE = "Asia/Kolkata";

export function zoneName(zone: Zone, displayTz: string): string {
  return zone === "UTC" ? "UTC" : displayTz;
}

export function zoneLabel(zone: Zone, displayTz: string): string {
  if (zone === "UTC") return "UTC";
  return displayTz === "Asia/Kolkata" ? "IST" : displayTz;
}

const fmtCache = new Map<string, Intl.DateTimeFormat>();

function fmt(tz: string, withDate: boolean, ms: boolean): Intl.DateTimeFormat {
  const key = `${tz}|${withDate}|${ms}`;
  let f = fmtCache.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat("en-GB", {
      timeZone: tz,
      year: withDate ? "numeric" : undefined,
      month: withDate ? "short" : undefined,
      day: withDate ? "2-digit" : undefined,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      fractionalSecondDigits: ms ? 3 : undefined,
      hourCycle: "h23",
    });
    fmtCache.set(key, f);
  }
  return f;
}

export function formatTime(iso: string | number | Date, tz: string, withDate = true, ms = false): string {
  const d = iso instanceof Date ? iso : new Date(iso);
  return fmt(tz, withDate, ms).format(d);
}

export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 0) return "in the future";
  if (seconds < 1) return "just now";
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} h ago`;
  return `${(seconds / 86400).toFixed(1)} d ago`;
}

export function formatDuration(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} h`;
  return `${(seconds / 86400).toFixed(1)} d`;
}

/** Value for <input type="datetime-local"> in the given zone. */
export function toLocalInput(ms: number, tz: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(ms));
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "00";
  return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}`;
}

/** Parse a datetime-local value as wall time in ``tz``; returns epoch ms or NaN. */
export function fromLocalInput(value: string, tz: string): number {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!m) return NaN;
  const [y, mo, d, h, mi] = m.slice(1).map(Number) as [number, number, number, number, number];
  const guess = Date.UTC(y, mo - 1, d, h, mi);
  // offset of tz at that instant (two passes handle DST edges for zones that have them)
  let ts = guess;
  for (let i = 0; i < 2; i++) {
    const shown = toLocalInput(ts, tz);
    const sm = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(shown)!;
    const [sy, smo, sd, sh, smi] = sm.slice(1).map(Number) as [number, number, number, number, number];
    ts += guess - Date.UTC(sy, smo - 1, sd, sh, smi);
  }
  return ts;
}

export function isoUtc(ms: number): string {
  return new Date(ms).toISOString();
}
