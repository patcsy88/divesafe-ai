import type { AssessmentView, RunStatusView } from "./types.ts";

// What the API sent back for an assessment address: a finished assessment, or a run that has not
// produced one. Only the four known statuses are accepted, and each must have the shape that goes
// with it. Anything else is null, which callers show as an error, never as a result.
export type Fetched =
  | { kind: "assessment"; view: AssessmentView }
  | { kind: "run"; view: RunStatusView };

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export function classify(body: unknown): Fetched | null {
  if (!isObject(body)) return null;
  const status = body.status;
  if (status === "PENDING_HUMAN" || status === "DECIDED") {
    const record = body.record;
    if (!isObject(record)) return null;
    if (typeof record.id !== "string" || typeof record.final_recommendation !== "string") return null;
    // The API derives the status from the decision. If they disagree, trust neither.
    const decided = record.human_decision !== null && record.human_decision !== undefined;
    if (decided !== (status === "DECIDED")) return null;
    return { kind: "assessment", view: body as unknown as AssessmentView };
  }
  if (status === "RUNNING" || status === "FAILED") {
    // A run never carries a result. If one is present the reply is not what it claims to be.
    if ("record" in body || "final_recommendation" in body || "recommendation" in body) return null;
    if (typeof body.id !== "string" || typeof body.status_note !== "string") return null;
    return { kind: "run", view: body as unknown as RunStatusView };
  }
  return null;
}

// Fixed wording for the failure codes the API can store. An unknown code gets the generic line.
const FAILURE_TEXT: Record<string, string> = {
  timeout: "It took longer than the time allowed.",
  storage: "The result could not be stored.",
  internal: "The service hit an internal error.",
  shutdown: "The service was stopping.",
  start_passed: "The planned start time passed before the assessment began.",
};

export function failureText(code: string | null): string {
  return (code !== null && FAILURE_TEXT[code]) || "The reason is not known.";
}
