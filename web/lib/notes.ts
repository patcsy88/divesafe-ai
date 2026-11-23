import { BLANK_BRAILLE } from "./observations.ts";
import { plain } from "./present.ts";

// Display only: which fixed sentences the assessment page shows. Nothing here changes a result.

// ADR 0022 decision 14: the site-document notes reach the person. The API sends "" when the
// record says nothing about site documents. A missing or non-string field (an older or
// mismatched service) is not read as "nothing to say".
export const KNOWLEDGE_NOTE_MISSING =
  "The service did not say whether site documents were used. Do not assume site hazards were checked.";

export function knowledgeText(note: unknown): string | null {
  if (typeof note !== "string") return KNOWLEDGE_NOTE_MISSING;
  const text = plain(note).trim();
  return text === "" ? null : text;
}

// ADR 0008 Amendment 1: a GO or CAUTION without the degraded-evidence field could rest on model
// or forecast data, so it is treated as if it does (fails closed).
export const DEGRADED_NOTE_MISSING =
  "The service did not say whether this result rests on model or forecast data. Treat it as if it does.";

export function degradedNoteMissing(recommendation: string, note: unknown): boolean {
  return (recommendation === "GO" || recommendation === "CAUTION") && typeof note !== "string";
}

// An override reason with no visible text is shown as such, never as an empty row. Visible means
// a letter, digit, punctuation or symbol other than the blank fillers.
export const NO_VISIBLE_REASON = "(no visible text: treat as no reason given)";
const VISIBLE = new RegExp(`(?![\\p{Default_Ignorable_Code_Point}${BLANK_BRAILLE}])[\\p{L}\\p{N}\\p{P}\\p{S}]`, "u");

export function reasonShown(rationale: string): string {
  const text = plain(rationale).trim();
  return VISIBLE.test(text) ? text : NO_VISIBLE_REASON;
}

// Conflicting items set aside before the rules ran; absent on an older service.
export function withheldCount(items: unknown): number {
  return Array.isArray(items) ? items.length : 0;
}
