import { RECOMMENDATIONS } from "./present.ts";
import type { AssessmentView, RunStatusView } from "./types.ts";

// What the API sent back for an assessment address: a finished assessment, or a run that has not
// produced one. Only the four known statuses are accepted, and each must have the shape that goes
// with it. Anything else is null, which callers show as an error, never as a result.
export type Fetched =
  | { kind: "assessment"; view: AssessmentView }
  | { kind: "run"; view: RunStatusView };

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isRecommendation = (value: unknown): boolean =>
  typeof value === "string" && (RECOMMENDATIONS as readonly string[]).includes(value);

const RUN_KEYS = new Set(["id", "status", "created_at", "failure_code", "status_note", "notice"]);

function classifyRun(body: Record<string, unknown>, status: "RUNNING" | "FAILED"): Fetched | null {
  // The API forbids extra fields on a run, so any other field means this is not a run reply.
  if (Object.keys(body).some((k) => !RUN_KEYS.has(k))) return null;
  if (typeof body.id !== "string" || typeof body.created_at !== "string") return null;
  if (typeof body.status_note !== "string" || typeof body.notice !== "string") return null;
  const code = body.failure_code;
  if (code !== null && typeof code !== "string") return null;
  if (status === "RUNNING" && code !== null) return null;
  return { kind: "run", view: body as unknown as RunStatusView };
}

function classifyAssessment(
  body: Record<string, unknown>,
  status: "PENDING_HUMAN" | "DECIDED" | "DECISION_WITHHELD",
): Fetched | null {
  const record = body.record;
  if (!isObject(record) || typeof record.id !== "string") return null;
  if (!isRecommendation(record.final_recommendation)) return null;
  if (!isRecommendation(record.deterministic_recommendation)) return null;
  const proposed = record.proposed_recommendation;
  if (proposed !== null && !isRecommendation(proposed)) return null;
  const decision = record.human_decision;
  if (decision !== null) {
    // A decision is a real object with a known outcome, or the reply is not trusted.
    if (!isObject(decision) || !isRecommendation(decision.decision)) return null;
    if (typeof decision.decided_by !== "string" || typeof decision.decided_at !== "string") return null;
  }
  const actual = record.actual_conditions;
  if (actual !== null && !isObject(actual)) return null;
  // The API derives the status from the decision. If they disagree, trust neither.
  if (status !== "DECISION_WITHHELD" && (decision !== null) !== (status === "DECIDED")) return null;
  // Whether the decision was hidden from this key must be stated, and agree with the status.
  if (typeof body.decision_withheld !== "boolean") return null;
  if (body.decision_withheld !== (status === "DECISION_WITHHELD")) return null;
  // A hidden decision, and the later report that needs one, cannot be shown.
  if (body.decision_withheld && (decision !== null || actual !== null)) return null;
  const override = body.overrides_to_less_severe;
  if (status === "DECISION_WITHHELD" ? override !== null : typeof override !== "boolean") return null;
  return { kind: "assessment", view: body as unknown as AssessmentView };
}

export function classify(body: unknown): Fetched | null {
  if (!isObject(body)) return null;
  const status = body.status;
  if (status === "PENDING_HUMAN" || status === "DECIDED" || status === "DECISION_WITHHELD") {
    return classifyAssessment(body, status);
  }
  if (status === "RUNNING" || status === "FAILED") return classifyRun(body, status);
  return null;
}

// The id a reply is about, so a caller can check it is the one it asked for.
export function idOf(fetched: Fetched): string {
  return fetched.kind === "run" ? fetched.view.id : fetched.view.record.id;
}

// Fixed wording for the failure codes the API can store. An unknown code gets the generic line.
const FAILURE_TEXT = new Map<string, string>([
  ["timeout", "It took longer than the time allowed."],
  ["storage", "The result could not be stored."],
  ["internal", "The service hit an internal error."],
  ["shutdown", "The service was stopping."],
  ["start_passed", "The planned start time passed before the assessment began. Choose a later start time."],
]);

export function failureText(code: string | null): string {
  return (code !== null && FAILURE_TEXT.get(code)) || "The reason is not known.";
}
