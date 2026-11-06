import type { ChannelStatus, SampleView } from "./types";

type ChannelLike = Pick<
  ChannelStatus,
  "data_type" | "display_precision" | "unit" | "enum_labels" | "display_format"
>;

/** Number only (no unit), honouring the channel's precision and notation. */
export function formatNumber(
  ch: Pick<ChannelLike, "display_precision" | "display_format">,
  v: number,
): string {
  if (!Number.isFinite(v)) return "—";
  if (ch.display_format === "scientific")
    return v.toExponential(Math.max(1, Math.min(ch.display_precision, 4)));
  return v.toFixed(ch.display_precision);
}

export function formatValue(ch: ChannelLike, v: SampleView | null, withUnit = true): string {
  if (!v) return "—";
  if (v.value === null)
    return v.quality === "bad" ? `bad · ${humanize(v.quality_reason ?? "")}` : "—";
  if (ch.data_type === "enum")
    return v.label ?? ch.enum_labels?.[String(v.value)] ?? String(v.value);
  if (ch.data_type === "bool") return v.value ? "On" : "Off";
  const num = typeof v.value === "number" ? formatNumber(ch, v.value) : String(v.value);
  return withUnit && ch.unit ? `${num} ${ch.unit}` : num;
}

export function humanize(code: string): string {
  return code.replace(/_/g, " ");
}

export function axisLabel(v: number, scientific: boolean): string {
  if (scientific) return v === 0 ? "0" : v.toExponential(0);
  const a = Math.abs(v);
  if (a !== 0 && (a >= 1e5 || a < 1e-3)) return v.toExponential(1);
  return String(Number(v.toPrecision(6)));
}
