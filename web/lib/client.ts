import { trustProxy } from "./config.ts";
import { forwardedAddress } from "./throttle.ts";

type HeaderReader = (name: string) => Promise<string | null>;

// Loaded only when the proxy is trusted, so code that never trusts it never needs a request.
const fromRequest: HeaderReader = async (name) => (await (await import("next/headers")).headers()).get(name);

let reader: HeaderReader = fromRequest;

// For tests, which run outside a request. Null puts the request's own headers back.
export function readHeadersWith(replacement: HeaderReader | null): void {
  reader = replacement ?? fromRequest;
}

// The person's address as Caddy set it, passed on to the API with every call so that the API's
// failure limits count each person and not this server (security review R2). Null unless the proxy
// is trusted and the header holds a real address.
export async function clientAddress(env: Record<string, string | undefined> = process.env): Promise<string | null> {
  if (!trustProxy(env)) return null;
  return forwardedAddress(await reader("x-forwarded-for"), true);
}
