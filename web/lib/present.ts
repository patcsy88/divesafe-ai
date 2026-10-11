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
