import type { Recommendation } from "./types.ts";

// Display only. These choose a label and a style for what the API already decided.
export const RECOMMENDATIONS: readonly Recommendation[] = [
  "GO",
  "CAUTION",
  "NO-GO",
  "INSUFFICIENT EVIDENCE",
];

export function badgeClass(rec: string): string {
  switch (rec) {
    case "GO":
      return "badge go";
    case "CAUTION":
      return "badge caution";
    case "NO-GO":
      return "badge nogo";
    default:
      // Anything unrecognised is shown as inconclusive, never as safe.
      return "badge insufficient";
  }
}

export function labelFor(rec: string): string {
  return (RECOMMENDATIONS as readonly string[]).includes(rec) ? rec : "INSUFFICIENT EVIDENCE";
}

export function formatWhen(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "unknown time";
  return `${new Date(t).toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

// Strips control and format characters (including bidi overrides) from third-party text.
export function plain(text: string): string {
  return text.replace(/[^\P{Cc}\n]|\p{Cf}/gu, "");
}

export const MAX_SHOWN = 20_000;

// Third-party values as plain text, cut off at MAX_SHOWN characters.
export function shown(value: unknown): string {
  const text = JSON.stringify(value ?? null, null, 2) ?? "null";
  const clean = plain(text);
  return clean.length > MAX_SHOWN ? `${clean.slice(0, MAX_SHOWN)}\n… truncated` : clean;
}

export const MAX_TEXT = 1000;
export const MAX_WITHHELD_SHOWN = 50;

// A short third-party text as plain text, cut off at `max` characters. Display only.
export function clip(text: unknown, max: number = MAX_TEXT): string {
  const clean = plain(typeof text === "string" ? text : String(text ?? ""));
  return clean.length > max ? `${clean.slice(0, max)}… cut` : clean;
}

// Withheld items ordered by id (items sharing an id sit together), at most MAX_WITHHELD_SHOWN
// of them, and how many were left out. Display only.
export function capWithheld<T extends { id: string }>(items: readonly T[]): { shown: T[]; hidden: number } {
  const sorted = [...items].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  return { shown: sorted.slice(0, MAX_WITHHELD_SHOWN), hidden: Math.max(0, sorted.length - MAX_WITHHELD_SHOWN) };
}

const NOT_MEASURED = ["forecast", "model", "prediction"];

// What kind of statement this item is. Only an observation is called measured.
export function provenanceLabel(dataKind: string): string {
  if (dataKind === "observation") return "measured";
  if (NOT_MEASURED.includes(dataKind)) return `${dataKind}, not measured`;
  if (dataKind === "notice" || dataKind === "knowledge") return `${dataKind}: text, not a measurement`;
  return "kind unknown: not a measurement";
}

// Display order only, used to warn before a less severe choice. The service decides what an
// override is and records it; this never gates or changes anything.
const SEVERITY = ["GO", "CAUTION", "INSUFFICIENT EVIDENCE", "NO-GO"];

export function isLessSevere(choice: string, system: string): boolean {
  const c = SEVERITY.indexOf(choice);
  const s = SEVERITY.indexOf(system);
  return c !== -1 && s !== -1 && c < s;
}
