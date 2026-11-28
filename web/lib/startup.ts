import { apiBaseUrl, sessionSecret } from "./config.ts";
import { allowedHosts } from "./hosts.ts";

// Node runtime only (instrumentation.ts). Next only logs an error thrown from register() and goes
// on serving, so the process exits itself, with fixed text.
export function checkStartup(): void {
  try {
    sessionSecret();
    apiBaseUrl();
    allowedHosts();
  } catch {
    process.stderr.write("web: start-up check failed: session secret, API address or allowed hosts are invalid\n");
    process.exit(1);
  }
}
