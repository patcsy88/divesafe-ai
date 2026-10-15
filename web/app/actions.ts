"use server";

import { headers } from "next/headers";
import { redirect } from "next/navigation";
import {
  ApiError,
  checkKey,
  createAssessment,
  getAssessment,
  recordActualConditions,
  recordDecision,
} from "../lib/api.ts";
import { endSession, requireKey, startSession } from "../lib/auth.ts";
import { trustProxy } from "../lib/config.ts";
import { resolveConflict } from "../lib/conflict.ts";
import { parseObservations, sameObservations } from "../lib/observations.ts";
import { RECOMMENDATIONS } from "../lib/present.ts";
import type { AssessmentView } from "../lib/types.ts";
import { parsePositive, toAwareIso } from "../lib/time.ts";
import { forwardedAddress, LoginThrottle, sourceOf } from "../lib/throttle.ts";

export interface FormState {
  error: string | null;
  // Set when the earlier attempt is finished with, so the form should start a new one.
  renewKey?: boolean;
}

const text = (data: FormData, name: string): string => {
  const value = data.get(name);
  return typeof value === "string" ? value.trim() : "";
};

const throttle = ((globalThis as { __divesafeThrottle?: LoginThrottle }).__divesafeThrottle ??=
  new LoginThrottle());

export async function login(_prev: FormState, data: FormData): Promise<FormState> {
  const forwarded = (await headers()).get("x-forwarded-for");
  const source = sourceOf(forwarded, trustProxy());
  const key = text(data, "api_key");
  if (!/^[\x21-\x7e]{8,512}$/.test(key)) return { error: "Enter your personal API key." };
  // Counted as a failure before the network call and taken back unless the key is rejected, so a
  // burst of parallel guesses cannot all pass the check first.
  const wait = throttle.reserve(source, Date.now());
  if (wait !== null) {
    return { error: `Too many failed sign-ins. Wait ${wait} seconds and try again.` };
  }
  try {
    await checkKey(key, forwardedAddress(forwarded, trustProxy()) ?? undefined);
  } catch (error) {
    // Only a rejected key stays counted. An unreachable service or a 429 is not the person's mistake.
    if (!(error instanceof ApiError && error.status === 401)) throttle.release(source);
    return { error: error instanceof ApiError ? error.message : "Sign-in failed." };
  }
  throttle.release(source);
  await startSession(key);
  redirect("/");
}

export async function logout(): Promise<void> {
  await endSession();
  redirect("/login");
}

export async function runAssessment(_prev: FormState, data: FormData): Promise<FormState> {
  const key = await requireKey();
  const site = text(data, "site_id");
  if (!site) return { error: "Choose a registered site." };
  const start = toAwareIso(text(data, "planned_start"), text(data, "zone"));
  if (!start) return { error: "Enter a start date and time, and say which time zone it is in." };
  const minutes = parsePositive(text(data, "duration_minutes"), true);
  if (minutes === null || minutes > 24 * 60) {
    return { error: "Duration must be a whole number of minutes, up to 1440." };
  }
  const depth = parsePositive(text(data, "max_depth_m"), false);
  if (depth === null || depth > 1000) return { error: "Maximum depth must be a positive number of metres." };
  const decider = text(data, "decider");

  let id: string;
  try {
    id = await createAssessment(
      key,
      {
        site_id: site,
        planned_start: start,
        planned_duration_minutes: minutes,
        max_depth_m: depth,
        ...(decider ? { decider } : {}),
      },
      text(data, "retry_key"),
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await endSession();
      redirect("/login");
    }
    return {
      error: error instanceof ApiError ? error.message : "Could not run the assessment.",
      renewKey: error instanceof ApiError && error.status === 409,
    };
  }
  redirect(`/assessments/${encodeURIComponent(id)}`);
}

const ASSESSMENT_ID = /^[0-9a-f]{32}$/;

async function failed(
  error: unknown,
  fallback: string,
  key: string,
  id: string,
  recorded: (view: AssessmentView) => boolean,
  differs?: (view: AssessmentView) => string | null,
): Promise<FormState> {
  if (error instanceof ApiError && error.status === 401) {
    await endSession();
    redirect("/login");
  }
  if (error instanceof ApiError && error.status === 409) {
    const outcome = await resolveConflict(error, () => getAssessment(key, id), recorded, differs);
    if (outcome.kind === "login") {
      await endSession();
      redirect("/login");
    }
    if (outcome.kind === "record") redirect(`/assessments/${id}`);
    return { error: outcome.error, ...(outcome.renewKey ? { renewKey: true } : {}) };
  }
  return { error: error instanceof ApiError ? error.message : fallback };
}

export async function submitDecision(_prev: FormState, data: FormData): Promise<FormState> {
  const key = await requireKey();
  const id = text(data, "assessment_id");
  if (!ASSESSMENT_ID.test(id)) return { error: "Unknown assessment." };
  const decision = text(data, "decision");
  if (!(RECOMMENDATIONS as readonly string[]).includes(decision)) {
    return { error: "Choose the decision you are making." };
  }
  const rationale = text(data, "rationale");
  if (rationale.length > 5000) return { error: "The rationale is too long (5000 characters at most)." };
  try {
    // The API compares this with the system's recommendation and demands a rationale for an override.
    await recordDecision(
      key,
      id,
      { decision, ...(rationale ? { rationale } : {}) },
      text(data, "retry_key"),
    );
  } catch (error) {
    return failed(
      error,
      "Could not record the decision.",
      key,
      id,
      (v) =>
        v.record.human_decision?.decision === decision &&
        (v.record.human_decision.override_rationale ?? "") === rationale,
      (v) =>
        v.record.human_decision
          ? `A different decision is already on record: ${v.record.human_decision.decision}. Yours was not recorded.`
          : null,
    );
  }
  redirect(`/assessments/${id}`);
}

export async function submitActualConditions(_prev: FormState, data: FormData): Promise<FormState> {
  const key = await requireKey();
  const id = text(data, "assessment_id");
  if (!ASSESSMENT_ID.test(id)) return { error: "Unknown assessment." };
  const parsed = parseObservations((name) => text(data, name));
  if (!parsed.ok) return { error: parsed.error };
  try {
    await recordActualConditions(key, id, parsed.observations);
  } catch (error) {
    return failed(
      error,
      "Could not record the conditions.",
      key,
      id,
      (v) => sameObservations(v.record.actual_conditions?.observations, parsed.observations),
      (v) =>
        v.record.actual_conditions
          ? "Different conditions were already reported. Yours were not recorded."
          : null,
    );
  }
  redirect(`/assessments/${id}`);
}
