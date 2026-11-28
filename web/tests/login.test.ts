import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { checkKey } from "../lib/api.ts";
import { readHeadersWith } from "../lib/client.ts";

const real = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = real;
});

const reply = (status: number, body: unknown) => {
  globalThis.fetch = (async () =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })) as typeof fetch;
};

test("a key the API accepts signs in", async () => {
  reply(200, { sites: [] });
  await checkKey("a-valid-key-123");
});

test("a key with only another role is accepted when the API itself refuses it", async () => {
  reply(403, { detail: "nothing was recorded; this key is not permitted to do that" });
  await checkKey("assessor-only-key");
});

test("a 403 from something other than the API does not sign anyone in", async () => {
  reply(403, { detail: "Forbidden by proxy" });
  await assert.rejects(() => checkKey("any-string-at-all"));
  reply(403, null);
  await assert.rejects(() => checkKey("any-string-at-all"));
});

test("an unknown key and an unreachable API do not sign in", async () => {
  reply(401, { detail: "authentication required" });
  await assert.rejects(() => checkKey("wrong-key-xxxx"), /not accept/);
  globalThis.fetch = (async () => {
    throw new Error("down");
  }) as typeof fetch;
  await assert.rejects(() => checkKey("wrong-key-xxxx"), /did not answer/);
});

// Security review R2: the address goes with every API call, and only when the proxy is trusted.
test("the person's address is passed on only when the proxy is trusted and there is one", async () => {
  const seen: Array<string | null> = [];
  globalThis.fetch = (async (_url: unknown, init?: RequestInit) => {
    seen.push(new Headers(init?.headers).get("x-forwarded-for"));
    return new Response("{}", { status: 200 });
  }) as typeof fetch;
  const before = process.env.DIVESAFE_WEB_TRUST_PROXY;
  try {
    readHeadersWith(async () => "203.0.113.9");
    process.env.DIVESAFE_WEB_TRUST_PROXY = "1";
    await checkKey("a-valid-key-123");
    readHeadersWith(async () => null);
    await checkKey("a-valid-key-123");
    readHeadersWith(async () => "not-an-address");
    await checkKey("a-valid-key-123");
    readHeadersWith(async () => "203.0.113.9");
    delete process.env.DIVESAFE_WEB_TRUST_PROXY;
    await checkKey("a-valid-key-123");
    process.env.DIVESAFE_WEB_TRUST_PROXY = "0";
    await checkKey("a-valid-key-123");
  } finally {
    readHeadersWith(null);
    if (before === undefined) delete process.env.DIVESAFE_WEB_TRUST_PROXY;
    else process.env.DIVESAFE_WEB_TRUST_PROXY = before;
  }
  assert.deepEqual(seen, ["203.0.113.9", null, null, null, null]);
});

test("a rate-limit answer from the API is not shown as a wrong key", async () => {
  reply(429, { detail: "nothing was recorded; too many failed attempts; wait and try again" });
  await assert.rejects(() => checkKey("any-key-12345678"), (e: unknown) => (e as { status?: number }).status === 429);
});

test("a server error at sign-in says neither that nothing was recorded nor that something may have been", async () => {
  const fair = (error: Error) =>
    /could not be checked right now/.test(error.message) && !/Nothing was recorded|whether it was recorded/i.test(error.message);
  for (const status of [500, 502, 503, 504]) {
    for (const body of [{ detail: "storage is unavailable; nothing was recorded" }, { detail: "x" }, null]) {
      reply(status, body);
      await assert.rejects(() => checkKey("a-valid-key-123"), fair, String(status));
    }
  }
  globalThis.fetch = (async () => {
    throw new Error("down");
  }) as typeof fetch;
  await assert.rejects(() => checkKey("a-valid-key-123"), fair);
});

// Security review R2, and risk review round 2: every call carries the person's address, and web
// sends only these headers, so a client's own X-Request-ID (which the API keeps from a trusted
// proxy) or anything else it sent never reaches the API.
test("every API call sends exactly the expected headers, with the person's address", async () => {
  const { createAssessment, getAssessment, getEvidence, listSites, recordDecision, recordActualConditions } =
    await import("../lib/api.ts");
  const id = "0123456789abcdef0123456789abcdef";
  const retry = "00000000-0000-4000-8000-000000000000";
  const seen: Array<[string, Record<string, string>]> = [];
  globalThis.fetch = (async (url: unknown, init?: RequestInit) => {
    seen.push([String(url).replace(/^https?:\/\/[^/]+/, ""), Object.fromEntries(new Headers(init?.headers))]);
    const path = String(url);
    const body = path.endsWith("/evidence") ? { assessment_id: id } : { id, status: "PENDING_HUMAN" };
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  const before = process.env.DIVESAFE_WEB_TRUST_PROXY;
  try {
    readHeadersWith(async (name) => (name === "x-forwarded-for" ? "203.0.113.9" : "attacker-value"));
    process.env.DIVESAFE_WEB_TRUST_PROXY = "1";
    const calls: Array<() => Promise<unknown>> = [
      () => checkKey("a-valid-key-123"),
      () => listSites("a-valid-key-123"),
      () => getAssessment("a-valid-key-123", id),
      () => getEvidence("a-valid-key-123", id),
      () => createAssessment("a-valid-key-123", {} as never, retry),
      () => recordDecision("a-valid-key-123", id, { decision: "NO-GO" }, retry),
      () => recordActualConditions("a-valid-key-123", id, { wave_height_m: 1 }),
    ];
    for (const run of calls) await run().catch(() => undefined);
  } finally {
    readHeadersWith(null);
    if (before === undefined) delete process.env.DIVESAFE_WEB_TRUST_PROXY;
    else process.env.DIVESAFE_WEB_TRUST_PROXY = before;
  }
  assert.equal(seen.length, 7, seen.map(([p]) => p).join(" "));
  const read = { accept: "application/json", authorization: "Bearer a-valid-key-123", "x-forwarded-for": "203.0.113.9" };
  const write = { ...read, "content-type": "application/json" };
  assert.deepEqual(
    seen.map(([, h]) => h),
    [read, read, read, read, { ...write, "idempotency-key": retry }, { ...write, "idempotency-key": retry }, write],
  );
});
