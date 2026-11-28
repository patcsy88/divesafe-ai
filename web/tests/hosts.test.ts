import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { allowedHosts, hostAllowed, LOOPBACK_HOST } from "../lib/hosts.ts";

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

const LIST = { DIVESAFE_WEB_ALLOWED_HOSTS: "lan.home.arpa, Public.example.org,127.0.0.1:3100" };
const ALLOWED = allowedHosts(LIST);

test("the allowed names come from DIVESAFE_WEB_ALLOWED_HOSTS, trimmed and lower-cased (#70b)", () => {
  assert.deepEqual([...ALLOWED].sort(), ["127.0.0.1:3100", "lan.home.arpa", "public.example.org"]);
  assert.equal(LOOPBACK_HOST, "127.0.0.1:3100");
  assert.ok(hostAllowed("lan.home.arpa", null, ALLOWED));
  assert.ok(hostAllowed("LAN.home.arpa", "lan.home.arpa", ALLOWED));
  assert.ok(hostAllowed("public.example.org", "public.example.org", ALLOWED));
  assert.ok(hostAllowed("127.0.0.1:3100", null, ALLOWED));
});

test("a missing, empty or malformed list, or a wildcard, is refused", () => {
  for (const value of [undefined, "", " ", ",", "lan.home.arpa,", "*.home.arpa", "lan.home.arpa,*",
    "http://lan.home.arpa", "lan.home.arpa/x", "lan home.arpa", "-lan.home.arpa", "lan.home.arpa:0",
    "lan.home.arpa:99999x", "[::1]:3100", "lan..home.arpa", "a".repeat(254)]) {
    assert.throws(() => allowedHosts({ DIVESAFE_WEB_ALLOWED_HOSTS: value }), /DIVESAFE_WEB_ALLOWED_HOSTS/, String(value));
  }
});

test("localhost:3100 is no longer allowed by default", () => {
  assert.equal(hostAllowed("localhost:3100", null, ALLOWED), false);
});

test("a rebinding name, another port, a missing host or a bad forwarded host is refused", () => {
  for (const host of [
    "evil.example:3100", "evil.example", "127.0.0.1", "127.0.0.1:3101", "localhost:80",
    "127.0.0.1:3100.evil.example", "[::1]:3100", "", null, "lan.home.arpa:8443",
    // Refused on purpose (security N-e): Next compares names without normalising them either.
    "localhost.:3100", "127.0.0.1.:3100", "127.0.0.1:03100", "lan.home.arpa.",
  ]) {
    assert.equal(hostAllowed(host, null, ALLOWED), false, String(host));
  }
  assert.equal(hostAllowed("lan.home.arpa", "evil.example:3100", ALLOWED), false);
  assert.equal(hostAllowed("lan.home.arpa", "", ALLOWED), false);
});

// Security review R5: through Caddy a forwarded host is always present, and the loopback name is
// only for web's own healthcheck inside the container.
test("the loopback name is accepted only without a forwarded host", () => {
  assert.equal(hostAllowed("lan.home.arpa", "127.0.0.1:3100", ALLOWED), false);
  assert.equal(hostAllowed("127.0.0.1:3100", "lan.home.arpa", ALLOWED), false);
  assert.equal(hostAllowed("127.0.0.1:3100", "127.0.0.1:3100", ALLOWED), false);
});

// proxy.ts and instrumentation.ts sit outside the folders the hiding checks read, so their whole
// text is pinned here: any change is a reviewed change to this test.
test("proxy.ts refuses every request whose host is not allowed, with no matcher to narrow it", () => {
  assert.equal(
    read("../proxy.ts"),
    'import { NextResponse, type NextRequest } from "next/server";\n' +
      'import { hostAllowed } from "./lib/hosts.ts";\n\n' +
      "// Runs before every request, including static files and server actions.\n" +
      "export function proxy(request: NextRequest): NextResponse {\n" +
      '  if (!hostAllowed(request.headers.get("host"), request.headers.get("x-forwarded-host"))) {\n' +
      "    return new NextResponse(null, { status: 421 });\n" +
      "  }\n" +
      "  return NextResponse.next();\n" +
      "}\n",
  );
});

test("instrumentation.ts runs the start-up checks", () => {
  assert.equal(
    read("../instrumentation.ts"),
    "// Runs once when the server starts. A missing session secret or a bad API address stops the\n" +
      "// server here, instead of showing as a failure on every request.\n" +
      "export async function register(): Promise<void> {\n" +
      '  if (process.env.NEXT_RUNTIME === "nodejs") {\n' +
      '    const { checkStartup } = await import("./lib/startup.ts");\n' +
      "    checkStartup();\n" +
      "  }\n" +
      "}\n",
  );
});

// Next only logs an error thrown from register() and keeps serving, so checkStartup() must exit
// the process itself. Run in a child process with a throwaway environment.
test("a bad session secret, API address or host list stops the server with fixed text", () => {
  const run = (env: Record<string, string | undefined>) =>
    spawnSync(
      process.execPath,
      ["--experimental-strip-types", "--no-warnings", "-e",
        'import("./lib/startup.ts").then((m) => { m.checkStartup(); console.log("passed"); })'],
      {
        cwd: fileURLToPath(new URL("..", import.meta.url)),
        env: { PATH: process.env.PATH ?? "", ...env } as unknown as NodeJS.ProcessEnv,
        encoding: "utf8",
      },
    );
  const good = "zz-throwaway-test-secret-0123456789abcdef";
  const hosts = "lan.home.arpa,127.0.0.1:3100";
  for (const env of [{}, { DIVESAFE_WEB_SESSION_SECRET: "short", DIVESAFE_WEB_ALLOWED_HOSTS: hosts },
    { DIVESAFE_WEB_SESSION_SECRET: good, DIVESAFE_API_URL: "http://example.com:8000", DIVESAFE_WEB_ALLOWED_HOSTS: hosts },
    { DIVESAFE_WEB_SESSION_SECRET: good, DIVESAFE_API_URL: "http://127.0.0.1:8000" },
    { DIVESAFE_WEB_SESSION_SECRET: good, DIVESAFE_API_URL: "http://127.0.0.1:8000", DIVESAFE_WEB_ALLOWED_HOSTS: "" },
    { DIVESAFE_WEB_SESSION_SECRET: good, DIVESAFE_API_URL: "http://127.0.0.1:8000", DIVESAFE_WEB_ALLOWED_HOSTS: "*.home.arpa" }]) {
    const r = run(env);
    assert.equal(r.status, 1, JSON.stringify(env));
    assert.equal(r.stderr, "web: start-up check failed: session secret, API address or allowed hosts are invalid\n");
    assert.ok(!r.stderr.includes("short") && !r.stdout.includes("passed"));
  }
  const ok = run({ DIVESAFE_WEB_SESSION_SECRET: good, DIVESAFE_API_URL: "http://127.0.0.1:8000", DIVESAFE_WEB_ALLOWED_HOSTS: hosts });
  assert.equal(ok.status, 0);
  assert.equal(ok.stdout, "passed\n");
});
