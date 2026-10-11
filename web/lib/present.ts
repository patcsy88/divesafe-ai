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
