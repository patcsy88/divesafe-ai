// Server-only settings. Never import this from a client component.
const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]"]);

export function apiBaseUrl(env: Record<string, string | undefined> = process.env): string {
  const raw = (env.DIVESAFE_API_URL ?? "http://127.0.0.1:8000").trim();
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error("DIVESAFE_API_URL is not a valid URL");
  }
  if (url.protocol !== "https:" && !(url.protocol === "http:" && LOOPBACK.has(url.hostname))) {
    throw new Error("DIVESAFE_API_URL must be https, or http on loopback only");
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error("DIVESAFE_API_URL must not carry credentials, a query or a fragment");
  }
  return `${url.protocol}//${url.host}${url.pathname.replace(/\/+$/, "")}`;
}

export function sessionSecret(env: Record<string, string | undefined> = process.env): string {
  const secret = env.DIVESAFE_WEB_SESSION_SECRET ?? "";
  if (secret.length < 32) {
    throw new Error("DIVESAFE_WEB_SESSION_SECRET must be set to at least 32 characters");
  }
  return secret;
}

export const SESSION_MAX_AGE_SECONDS = 8 * 60 * 60;
export const API_TIMEOUT_MS = 120_000;
