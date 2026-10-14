import { API_TIMEOUT_MS, apiBaseUrl } from "./config.ts";
import { plain } from "./present.ts";
import { classify, idOf, type Fetched } from "./status.ts";
import type { AssessmentView, EvidenceView, SiteListView } from "./types.ts";

export class ApiError extends Error {
  status: number;
  refused: boolean;
  constructor(status: number, message: string, refused = false) {
    super(message);
    this.status = status;
    this.refused = refused;
  }
}

const ID = /^[0-9a-f]{32}$/;

export function describeFailure(status: number, detail: unknown, retryAfter: string | null): string {
  if (status === 401) return "The server did not accept this API key.";
  if (status === 403) {
    return "Not permitted: this key lacks the role, or you are not the person this assessment is assigned to.";
  }
  if (status === 404) return "Not found.";
  if (status === 409) {
    return typeof detail === "string" ? plain(detail).slice(0, 300) : "That cannot be done in the current state.";
  }
  if (status === 429) return `Too many requests. Wait ${retryAfter ?? "a moment"} seconds and retry.`;
  if (status === 422) {
    if (typeof detail === "string") return plain(detail).slice(0, 300);
    if (Array.isArray(detail)) {
      return detail
        .slice(0, 5)
        .map((e: unknown) => {
          const item = (e ?? {}) as { loc?: unknown; msg?: unknown };
          const where = Array.isArray(item.loc) ? item.loc.join(".") : "";
          return `${plain(where)}: ${plain(String(item.msg ?? "invalid")).slice(0, 120)}`;
        })
        .join("; ");
    }
    return "The request was not accepted.";
  }
  if (status === 503) return "The service is unavailable. Nothing was recorded.";
  return "Something went wrong. The outcome is unknown: check whether it was recorded before trying again.";
}

async function call(
  path: string,
  key: string,
  init: RequestInit = {},
  source?: string,
  idempotencyKey?: string,
): Promise<unknown> {
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
        ...(source ? { "X-Forwarded-For": source } : {}),
        ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
      },
    });
  } catch {
    throw new ApiError(0, "The DiveSafe service did not answer, so the outcome is unknown: check whether it was recorded before trying again.");
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
      typeof detail === "string" && detail.startsWith("nothing was recorded"),
    );
  }
  return body;
}

// `source` is the person's address, passed on so the API counts failures against them and not
// against this server. The API believes it only if it is configured to trust this server.
export async function checkKey(key: string, source?: string): Promise<void> {
  // 401 means the key is unknown. 403 means it is valid but lacks the viewer role, which is
  // acceptable here: roles are independent and an assessor may not be able to read.
  try {
    await call("/v1/sites", key, {}, source);
  } catch (error) {
    if (error instanceof ApiError && error.status === 403 && error.refused) return;
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

// A retry key is sent so that repeating a request whose outcome was unknown returns the original
// result instead of making a second record. Only a well-formed key is sent.
export const RETRY_KEY = /^[A-Za-z0-9._-]{16,128}$/;

const UNREADABLE =
  "The service sent a reply this page does not recognise, so nothing is shown as a result. Check whether the assessment was recorded.";

// The API now answers a create with 202 and a run to poll, or with 200 and the finished record
// when the same retry key was used before. Either way only the id is taken from it; the result is
// always read afterwards from the assessment address.
export async function createAssessment(
  key: string,
  body: CreateBody,
  retryKey?: string,
): Promise<string> {
  const reply = classify(
    await call(
      "/v1/assessments",
      key,
      { method: "POST", body: JSON.stringify(body) },
      undefined,
      retryKey && RETRY_KEY.test(retryKey) ? retryKey : undefined,
    ),
  );
  if (reply === null) throw new ApiError(0, UNREADABLE);
  const id = idOf(reply);
  if (!ID.test(id)) throw new ApiError(0, UNREADABLE);
  return id;
}

export async function getAssessment(key: string, id: string): Promise<Fetched> {
  if (!ID.test(id)) throw new ApiError(404, "Not found.");
  let body: unknown;
  try {
    body = await call(`/v1/assessments/${encodeURIComponent(id)}`, key);
  } catch (error) {
    // A failed read changes nothing, so "nothing was recorded" would be untrue: the run may well
    // still finish.
    if (error instanceof ApiError && error.status === 503) {
      throw new ApiError(503, "Could not check right now, so the outcome is unknown. Try again shortly.");
    }
    throw error;
  }
  const reply = classify(body);
  // A reply about some other assessment must never be shown (or decided) under this address.
  if (reply === null || idOf(reply) !== id) throw new ApiError(0, UNREADABLE);
  return reply;
}

export async function getEvidence(key: string, id: string): Promise<EvidenceView> {
  if (!ID.test(id)) throw new ApiError(404, "Not found.");
  return (await call(`/v1/assessments/${encodeURIComponent(id)}/evidence`, key)) as EvidenceView;
}

export async function listSites(key: string): Promise<SiteListView> {
  return (await call("/v1/sites", key)) as SiteListView;
}

export async function recordDecision(
  key: string,
  id: string,
  body: { decision: string; rationale?: string },
  retryKey?: string,
): Promise<AssessmentView> {
  if (!ID.test(id)) throw new ApiError(404, "Not found.");
  return (await call(
    `/v1/assessments/${encodeURIComponent(id)}/decision`,
    key,
    { method: "POST", body: JSON.stringify(body) },
    undefined,
    retryKey && RETRY_KEY.test(retryKey) ? retryKey : undefined,
  )) as AssessmentView;
}

export async function recordActualConditions(
  key: string,
  id: string,
  observations: Record<string, string | number>,
): Promise<AssessmentView> {
  if (!ID.test(id)) throw new ApiError(404, "Not found.");
  return (await call(`/v1/assessments/${encodeURIComponent(id)}/actual-conditions`, key, {
    method: "POST",
    body: JSON.stringify({ observations }),
  })) as AssessmentView;
}
