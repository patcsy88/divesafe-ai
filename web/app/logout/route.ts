import { endSession } from "../../lib/auth.ts";

// Reached when the API rejects a stored key, so a revoked key does not look signed in.
// The redirect is relative so it stays on whatever origin the person is using.
// A link elsewhere, including another service on this machine (same site, other port), cannot
// sign the person out: only this app's own pages, or a typed address, end the session. A browser
// that sends no Sec-Fetch-Site is let through. A refused request gets fixed text, not a redirect:
// a redirect back to a page whose key the API rejects would loop.
const OWN = new Set(["same-origin", "none"]);
const REFUSED =
  "Sign-out was not done because the request came from another site. Open DiveSafe directly.\n";

export async function GET(request: Request): Promise<Response> {
  const site = request.headers.get("sec-fetch-site");
  if (site !== null && !OWN.has(site)) {
    return new Response(REFUSED, {
      status: 200,
      headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
    });
  }
  await endSession();
  return new Response(null, { status: 307, headers: { location: "/login" } });
}
