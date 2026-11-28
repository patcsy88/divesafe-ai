// The host names this server answers to (owner #70b, ADR 0017 Caddy amendment). A request for any
// other name, as a DNS-rebinding page would send, is refused before it reaches a page or an
// action. The names come from DIVESAFE_WEB_ALLOWED_HOSTS; a missing or empty list stops start-up.
export const LOOPBACK_HOST = "127.0.0.1:3100";

const NAME = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*(?::[1-9][0-9]{0,4})?$/;

export function allowedHosts(env: Record<string, string | undefined> = process.env): ReadonlySet<string> {
  const names = (env.DIVESAFE_WEB_ALLOWED_HOSTS ?? "").split(",").map((n) => n.trim().toLowerCase());
  if (names.length === 0 || names.some((n) => n.length === 0 || n.length > 253 || !NAME.test(n))) {
    throw new Error("DIVESAFE_WEB_ALLOWED_HOSTS must be a comma-separated list of host names");
  }
  return new Set(names);
}

// The loopback name is web's own healthcheck inside the container, which sends no forwarded host.
// Through Caddy, both the host and the forwarded host must be one of the listed names, never it.
export function hostAllowed(
  host: string | null,
  forwardedHost: string | null,
  allowed: ReadonlySet<string> = allowedHosts(),
): boolean {
  const h = (host ?? "").toLowerCase();
  if (!allowed.has(h)) return false;
  if (forwardedHost === null) return true;
  const f = forwardedHost.toLowerCase();
  return h !== LOOPBACK_HOST && f !== LOOPBACK_HOST && allowed.has(f);
}
