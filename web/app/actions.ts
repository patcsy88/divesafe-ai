"use server";

import { redirect } from "next/navigation";
import { ApiError, checkKey, createAssessment } from "../lib/api.ts";
import { endSession, requireKey, startSession } from "../lib/auth.ts";
import { SITES } from "../lib/sites.ts";
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
    await checkKey(key, SITES[0]!.id);
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
  if (!SITES.some((s) => s.id === site)) return { error: "Choose a registered site." };
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
