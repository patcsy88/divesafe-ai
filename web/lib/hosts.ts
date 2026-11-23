// The host names this server answers to (owner #66a, #70b: this Mac only). A request for any
// other name, as a DNS-rebinding page would send, is refused before it reaches a page or an
// action. Remote access through a proxy (owner #67a, #68a) replaces this list with a setting.
export const ALLOWED_HOSTS: ReadonlySet<string> = new Set(["127.0.0.1:3100", "localhost:3100"]);

export function hostAllowed(host: string | null, forwardedHost: string | null): boolean {
  if (!ALLOWED_HOSTS.has((host ?? "").toLowerCase())) return false;
  return forwardedHost === null || ALLOWED_HOSTS.has(forwardedHost.toLowerCase());
}
