import { ApiError } from "./api.ts";
import type { Fetched } from "./status.ts";
import type { AssessmentView } from "./types.ts";

// What a form should do after the API refused a submission with 409. The page only goes to the
// record when a fresh read shows what was submitted; a run with no record has nothing recorded.
export type ConflictOutcome =
  | { kind: "record" }
  | { kind: "login" }
  | { kind: "error"; error: string; renewKey?: boolean };

export async function resolveConflict(
  refusal: ApiError,
  read: () => Promise<Fetched>,
  recorded: (view: AssessmentView) => boolean,
  // What to tell the person when the record holds something other than what they submitted.
  differs?: (view: AssessmentView) => string | null,
): Promise<ConflictOutcome> {
  try {
    const fetched = await read();
    if (fetched.kind !== "assessment") return { kind: "error", error: refusal.message };
    const view = fetched.view;
    if (recorded(view)) return { kind: "record" };
    const other = differs?.(view) ?? null;
    if (other !== null) return { kind: "error", error: other, renewKey: true };
  } catch (inner) {
    if (inner instanceof ApiError && inner.status === 401) return { kind: "login" };
    if (inner instanceof ApiError) return { kind: "error", error: refusal.message };
    throw inner;
  }
  return { kind: "error", error: refusal.message };
}
