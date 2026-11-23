import { apiBaseUrl, sessionSecret } from "./config.ts";

// Node runtime only (instrumentation.ts). Next only logs an error thrown from register() and goes
// on serving, so the process exits itself, with fixed text.
export function checkStartup(): void {
  try {
    sessionSecret();
    apiBaseUrl();
  } catch {
    process.stderr.write("web: start-up check failed: session secret or API address is invalid\n");
    process.exit(1);
  }
}
