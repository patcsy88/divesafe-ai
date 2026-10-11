import { NextResponse } from "next/server";
import { endSession } from "../../lib/auth.ts";

// Reached when the API rejects a stored key, so a revoked key does not look signed in.
export async function GET(request: Request): Promise<NextResponse> {
  await endSession();
  return NextResponse.redirect(new URL("/login", request.url));
}
