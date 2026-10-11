import { endSession } from "../../lib/auth.ts";

// Reached when the API rejects a stored key, so a revoked key does not look signed in.
// The redirect is relative so it stays on whatever origin the person is using.
export async function GET(): Promise<Response> {
  await endSession();
  return new Response(null, { status: 307, headers: { location: "/login" } });
}
