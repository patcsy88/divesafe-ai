import { NextResponse, type NextRequest } from "next/server";
import { hostAllowed } from "./lib/hosts.ts";

// Runs before every request, including static files and server actions.
export function proxy(request: NextRequest): NextResponse {
  if (!hostAllowed(request.headers.get("host"), request.headers.get("x-forwarded-host"))) {
    return new NextResponse(null, { status: 421 });
  }
  return NextResponse.next();
}
