"use server";

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
import { parseObservations } from "../lib/observations.ts";
import { RECOMMENDATIONS } from "../lib/present.ts";
import { parsePositive, toAwareIso } from "../lib/time.ts";

export interface FormState {
  error: string | null;
}

const text = (data: FormData, name: string): string => {
  const value = data.get(name);
  return typeof value === "string" ? value.trim() : "";
};

export async function login(_prev: FormState, data: FormData): Promise<FormState> {
  const key = text(data, "api_key");
  if (!/^[\x21-\x7e]{8,512}$/.test(key)) return { error: "Enter your personal API key." };
  try {
    await checkKey(key);
  } catch (error) {
    return { error: error instanceof ApiError ? error.message : "Sign-in failed." };
  }
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
    const view = await createAssessment(key, {
      site_id: site,
      planned_start: start,
      planned_duration_minutes: minutes,
      max_depth_m: depth,
      ...(decider ? { decider } : {}),
    });
    id = view.record.id;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await endSession();
      redirect("/login");
    }
    return { error: error instanceof ApiError ? error.message : "Could not run the assessment." };
  }
  redirect(`/assessments/${encodeURIComponent(id)}`);
}

const ASSESSMENT_ID = /^[0-9a-f]{32}$/;

async function failed(
  error: unknown,
  fallback: string,
  key: string,
  id: string,
  recorded: (view: Awaited<ReturnType<typeof getAssessment>>) => boolean,
): Promise<FormState> {
  if (error instanceof ApiError && error.status === 401) {
    await endSession();
    redirect("/login");
  }
  if (error instanceof ApiError && error.status === 409) {
    // A second tab or a retry: only go to the record if a fresh read shows it was recorded.
    try {
      if (recorded(await getAssessment(key, id))) redirect(`/assessments/${id}`);
    } catch (inner) {
      if (inner instanceof ApiError) return { error: error.message };
      throw inner;
    }
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
    await recordDecision(key, id, { decision, ...(rationale ? { rationale } : {}) });
  } catch (error) {
    return failed(error, "Could not record the decision.", key, id, (v) => v.record.human_decision !== null);
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
    return failed(error, "Could not record the conditions.", key, id, (v) => v.record.actual_conditions !== null);
  }
  redirect(`/assessments/${id}`);
}
