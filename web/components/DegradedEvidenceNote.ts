import { createElement as h, type ReactElement } from "react";

// ADR 0008 Amendment 1: a GO or CAUTION that rests on DEGRADED evidence carries a fixed note for
// the human. Written with createElement (no JSX) so that the offline tests can render it. The
// page shows this app's own copy of the wording, never the API's string; a safety test reads this
// file and pins it equal to `DEGRADED_EVIDENCE_NOTE` in `src/divesafe/api/schemas.py`.
export const DEGRADED_EVIDENCE_NOTE = "based on model and forecast data, not observations";

// Shown only for a non-empty string from the API (the view sends "" otherwise).
export const showsDegradedNote = (note: unknown): boolean => typeof note === "string" && note !== "";

// The decision form's context sentence, so the note is also in front of the person deciding.
export const degradedContext = (note: unknown): string =>
  showsDegradedNote(note) ? ` This result is ${DEGRADED_EVIDENCE_NOTE}.` : "";

export default function DegradedEvidenceNote({ note }: { note: unknown }): ReactElement | null {
  if (!showsDegradedNote(note)) return null;
  return h(
    "p",
    { className: "ds-pending", role: "note", "data-testid": "degraded-evidence-note" },
    `This result is ${DEGRADED_EVIDENCE_NOTE}.`,
  );
}
