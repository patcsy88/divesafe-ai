import { API_TIMEOUT_MS, apiBaseUrl } from "./config.ts";
import type { AssessmentView } from "./types.ts";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

const ID = /^[0-9a-f]{32}$/;

export function describeFailure(status: number, detail: unknown, retryAfter: string | null): string {
  if (status === 401) return "The server did not accept this API key.";
  if (status === 403) return "This key does not have the role needed for that.";
  if (status === 404) return "Not found.";
  if (status === 429) return `Too many requests. Wait ${retryAfter ?? "a moment"} seconds and retry.`;
  if (status === 422) {
    if (typeof detail === "string") return detail.slice(0, 300);
    if (Array.isArray(detail)) {
      return detail
        .slice(0, 5)
        .map((e: unknown) => {
          const item = (e ?? {}) as { loc?: unknown; msg?: unknown };
          const where = Array.isArray(item.loc) ? item.loc.join(".") : "";
          return `${where}: ${String(item.msg ?? "invalid").slice(0, 120)}`;
        })
        .join("; ");
    }
    return "The request was not accepted.";
  }
  if (status === 503) return "The service is unavailable. Nothing was recorded.";
  return "Something went wrong. The outcome is unknown: check for a new assessment before retrying.";
}

async function call(path: string, key: string, init: RequestInit = {}): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl()}${path}`, {
      ...init,
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
      headers: {
        Accept: "application/json",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        Authorization: `Bearer ${key}`,
      },
    });
  } catch {
    throw new ApiError(0, "The DiveSafe service did not answer. The outcome is unknown: check for a new assessment before retrying.");
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    throw new ApiError(
      response.status,
      describeFailure(response.status, detail, response.headers.get("retry-after")),
    );
  }
  return body;
}

export async function checkKey(key: string, siteId: string): Promise<void> {
  // 401 means the key is unknown. 403 means it is valid but lacks the viewer role, which is
  // acceptable here: roles are independent and an assessor may not be able to read.
  try {
    await call(`/v1/sites/${encodeURIComponent(siteId)}`, key);
  } catch (error) {
    if (error instanceof ApiError && (error.status === 403 || error.status === 404)) return;
    throw error;
  }
}

export interface CreateBody {
  site_id: string;
  planned_start: string;
  planned_duration_minutes: number;
  max_depth_m: number;
  decider?: string;
}

export async function createAssessment(key: string, body: CreateBody): Promise<AssessmentView> {
  return (await call("/v1/assessments", key, {
    method: "POST",
    body: JSON.stringify(body),
  })) as AssessmentView;
}

export async function getAssessment(key: string, id: string): Promise<AssessmentView> {
  if (!ID.test(id)) throw new ApiError(404, "Not found.");
  return (await call(`/v1/assessments/${encodeURIComponent(id)}`, key)) as AssessmentView;
}
