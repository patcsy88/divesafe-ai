import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import { createServer, request, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, test } from "node:test";

// Security review R6 (owner #141a): serverActions.allowedOrigins stays unset, so Next compares the
// Origin with X-Forwarded-Host, which Caddy sets to the site's own name. Runs against the built
// server (`npm run build` first), with a stub in place of the API.
const LAN = "lan.example.home.arpa";
const PORT = 3198;
let api: Server;
let web: ChildProcess;
const reached: string[] = [];

async function waitFor(check: () => Promise<boolean>): Promise<void> {
  for (let i = 0; i < 120; i++) {
    if (await check().catch(() => false)) return;
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error("web did not start");
}

function send(
  method: string,
  headers: Record<string, string>,
  body?: string,
): Promise<{ status: number; text: string; cookies: string[] }> {
  return new Promise((resolve, reject) => {
    const req = request(
      { host: "127.0.0.1", port: PORT, method, path: "/login", headers: { Host: LAN, "X-Forwarded-Host": LAN, ...headers } },
      (res) => {
        let text = "";
        res.setEncoding("utf8");
        res.on("data", (d: string) => (text += d));
        res.on("end", () => resolve({ status: res.statusCode ?? 0, text, cookies: res.headers["set-cookie"] ?? [] }));
      },
    );
    req.on("error", reject);
    req.end(body);
  });
}

const unescape = (v: string) =>
  v.replace(/&quot;/g, '"').replace(/&#x27;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");

// What a browser sends when the sign-in form is submitted without JavaScript: the form's hidden
// fields as rendered, plus the key.
async function signIn(origin: string | null): Promise<{ status: number; cookies: string[] }> {
  const page = await send("GET", {});
  assert.equal(page.status, 200);
  const fields: Array<[string, string]> = [];
  for (const m of page.text.matchAll(/<input type="hidden" name="([^"]+)"(?: value="([^"]*)")?\/>/g)) {
    fields.push([unescape(m[1] ?? ""), unescape(m[2] ?? "")]);
  }
  assert.ok(fields.some(([n]) => n === "$ACTION_KEY"), "the sign-in form has no action");
  fields.push(["api_key", "a-valid-key-123"]);
  const boundary = "zzboundary7f3a";
  const body =
    fields.map(([n, v]) => `--${boundary}\r\nContent-Disposition: form-data; name="${n}"\r\n\r\n${v}\r\n`).join("") +
    `--${boundary}--\r\n`;
  const headers: Record<string, string> = {
    "Content-Type": `multipart/form-data; boundary=${boundary}`,
    "X-Forwarded-For": "203.0.113.9",
  };
  if (origin !== null) headers.Origin = origin;
  return send("POST", headers, body);
}

before(async () => {
  api = createServer((q, r) => {
    reached.push(`${q.url ?? ""} ${q.headers["x-forwarded-for"] ?? "-"}`);
    if ((q.url ?? "").startsWith("/v1/assessments/")) {
      r.writeHead(404, { "content-type": "application/json" });
      r.end('{"detail":"Not found"}');
      return;
    }
    r.writeHead(200, { "content-type": "application/json" });
    r.end('{"sites":[]}');
  });
  await new Promise<void>((r) => api.listen(0, "127.0.0.1", r));
  const apiPort = (api.address() as AddressInfo).port;
  web = spawn(process.execPath, ["node_modules/next/dist/bin/next", "start", "-H", "127.0.0.1", "-p", String(PORT)], {
    env: {
      PATH: process.env.PATH ?? "",
      NODE_ENV: "production",
      NEXT_TELEMETRY_DISABLED: "1",
      DIVESAFE_API_URL: `http://127.0.0.1:${apiPort}`,
      DIVESAFE_WEB_SESSION_SECRET: "e".repeat(64),
      DIVESAFE_WEB_ALLOWED_HOSTS: `${LAN},127.0.0.1:3100`,
      DIVESAFE_WEB_TRUST_PROXY: "1",
    },
    stdio: "ignore",
  });
  await waitFor(async () => (await send("GET", {})).status === 200);
});

after(async () => {
  web?.kill("SIGTERM");
  await new Promise<void>((r) => api.close(() => r()));
});

test("a sign-in from a foreign Origin is refused and nothing reaches the API", async () => {
  for (const origin of [
    "https://evil.example",
    `http://${LAN}:8443`,
    `https://${LAN}.evil.example`,
    "https://127.0.0.1:3100",
    "null",
  ]) {
    reached.length = 0;
    const { status } = await signIn(origin);
    assert.equal(status, 500, origin);
    assert.deepEqual(reached, [], origin);
  }
});

// The control: the same request from the site's own Origin does reach the API, so the refusal above
// is the Origin check and not a malformed request.
test("the same sign-in from the site's own Origin reaches the API, with the person's address", async () => {
  reached.length = 0;
  const { status, cookies } = await signIn(`https://${LAN}`);
  assert.ok(status === 200 || status === 303, String(status));
  assert.deepEqual(reached, ["/v1/sites 203.0.113.9"]);
  // Owner #139a: the real header a browser gets, not only the options passed to Next.
  const session = cookies.filter((c) => c.startsWith("__Host-divesafe_session="));
  assert.equal(session.length, 1, cookies.join(" | "));
  const parts = (session[0] ?? "").split(";").slice(1).map((p) => p.trim().toLowerCase());
  assert.deepEqual(parts.sort(), ["httponly", "path=/", "samesite=strict", "secure"]);
});

// Next lets a POST with no Origin through (no browser sends a cross-site form without one), so a
// request with no Origin is a hand-made one, which holds only what its sender already had.
test("a sign-in with no Origin is not refused by this check", async () => {
  reached.length = 0;
  const { status } = await signIn(null);
  assert.ok(status === 200 || status === 303, String(status));
  assert.equal(reached.length, 1);
});

// proxy.ts reads DIVESAFE_WEB_ALLOWED_HOSTS at request time in the built server.
test("a host or forwarded host outside the list is refused with 421 before any page", async () => {
  const cases: Array<Record<string, string>> = [
    { Host: "evil.example", "X-Forwarded-Host": "evil.example" },
    { Host: LAN, "X-Forwarded-Host": "evil.example" },
    { Host: LAN, "X-Forwarded-Host": "127.0.0.1:3100" },
    { Host: "localhost:3100" },
  ];
  for (const headers of cases) {
    reached.length = 0;
    assert.equal((await send("GET", headers)).status, 421, JSON.stringify(headers));
    assert.deepEqual(reached, []);
  }
});

// Owner #145a: Caddy sends no HSTS (tests/safety/test_caddyfile.py), so web must send exactly one.
test("every page sends exactly one HSTS header, a year, with no includeSubDomains or preload", async () => {
  const res = await new Promise<string[]>((resolve, reject) => {
    const req = request(
      { host: "127.0.0.1", port: PORT, method: "GET", path: "/login", headers: { Host: LAN, "X-Forwarded-Host": LAN } },
      (r) => {
        r.resume();
        r.on("end", () => {
          const values: string[] = [];
          for (let i = 0; i < r.rawHeaders.length; i += 2) {
            if (r.rawHeaders[i]?.toLowerCase() === "strict-transport-security") values.push(r.rawHeaders[i + 1] ?? "");
          }
          resolve(values);
        });
      },
    );
    req.on("error", reject);
    req.end();
  });
  assert.deepEqual(res, ["max-age=31536000"]);
});

// Risk review round 2: pages read from the API while rendering, so the person's address must be
// read there too, with the proxy trusted, and a page must still render.
test("pages that read from the API render and pass on the person's address", async () => {
  const { cookies } = await signIn(`https://${LAN}`);
  const cookie = (cookies.find((c) => c.startsWith("__Host-divesafe_session=")) ?? "").split(";")[0] ?? "";
  assert.ok(cookie.length > 30);
  const id = "0123456789abcdef0123456789abcdef";
  for (const [path, status, api] of [
    ["/", 200, "/v1/sites"],
    [`/assessments/${id}`, 404, `/v1/assessments/${id}`],
  ] as const) {
    reached.length = 0;
    const res = await new Promise<number>((resolve, reject) => {
      const req = request(
        {
          host: "127.0.0.1",
          port: PORT,
          method: "GET",
          path,
          headers: { Host: LAN, "X-Forwarded-Host": LAN, "X-Forwarded-For": "203.0.113.9", Cookie: cookie },
        },
        (r) => {
          r.resume();
          r.on("end", () => resolve(r.statusCode ?? 0));
        },
      );
      req.on("error", reject);
      req.end();
    });
    assert.equal(res, status, path);
    assert.deepEqual(reached, [`${api} 203.0.113.9`], path);
  }
});
