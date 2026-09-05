/* Display formatting. Engine timestamps are UTC ISO strings; the UI shows
   them as readable moments ("Sep 5 · 04:00") with the relation to the live
   head where it helps ("in 3d 4h"). Never reformats what it can't parse. */

import type { ShipmentLine, TrappedCargo } from "./api";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const two = (n: number) => String(n).padStart(2, "0");

/** "Sep 5 · 04:00" (UTC). Falls back to the raw string if unparseable. */
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()} · ${two(d.getUTCHours())}:${two(d.getUTCMinutes())}`;
}

/** "Sep 5" — the day only. */
export function fmtDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
}

/** Minutes → "2h 16m", "8d 21h", "45m". */
export function fmtDuration(minutes: number): string {
  const m = Math.max(0, Math.round(minutes));
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d}d ${h % 24}h` : `${d}d`;
}

/** "in 3d 4h" / "2h ago" / "now", relative to the live head. */
export function fmtRelative(iso: string | null | undefined, now: string | null): string {
  if (!iso || !now) return "";
  const delta = (new Date(iso).getTime() - new Date(now).getTime()) / 60000;
  if (!Number.isFinite(delta)) return "";
  if (Math.abs(delta) < 1) return "now";
  return delta > 0 ? `in ${fmtDuration(delta)}` : `${fmtDuration(-delta)} ago`;
}

/** Grams → "10.8 t" / "450 kg". */
export function fmtWeight(grams: number | null | undefined): string {
  if (!grams) return "";
  const kg = grams / 1000;
  return kg >= 1000 ? `${(kg / 1000).toFixed(1)} t` : `${Math.round(kg)} kg`;
}

/** Cents → "$1,504". */
export function fmtMoney(cents: number): string {
  return `$${Math.round(cents / 100).toLocaleString("en-US")}`;
}

/** The cargo in its own units: "24 units GENERAL-21", "+1 more" when several. */
export function fmtCargo(lines: ShipmentLine[]): string {
  if (lines.length === 0) return "";
  const [first, ...rest] = lines;
  const uom = first!.uom === "unit" && first!.quantity !== 1 ? "units" : first!.uom;
  const head = `${first!.quantity} ${uom} ${first!.sku}`;
  return rest.length ? `${head} +${rest.length} more` : head;
}

/** "2 trapped at SYD — manual clearing required", counted per facility. Empty
    string when a re-optimization stranded nothing, so a caller can append it
    unconditionally. */
export function fmtTrapped(trapped: TrappedCargo[]): string {
  if (trapped.length === 0) return "";
  const counts = new Map<string, number>();
  for (const entry of trapped) {
    counts.set(entry.facility_id, (counts.get(entry.facility_id) ?? 0) + 1);
  }
  const parts = [...counts].map(([facility, count]) => `${count} trapped at ${facility}`);
  return `${parts.join(", ")} — manual clearing required`;
}

/** Temperature band "−25…−18 °C". */
export function fmtTemp(band: [number, number] | null): string {
  if (!band) return "";
  return `${band[0]}…${band[1]} °C`.replace(/-/g, "−");
}
