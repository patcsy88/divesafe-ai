import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { createAssessment, recordDecision } from "../lib/api.ts";

const real = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = real;
});

const seen: Array<string | null> = [];
const capture = () => {
  seen.length = 0;
  globalThis.fetch = (async (_url: unknown, init?: RequestInit) => {
    seen.push(new Headers(init?.headers).get("idempotency-key"));
    return new Response("{}", { status: 200 });
  }) as typeof fetch;
};
const body = { site_id: "s", planned_start: "2026-10-10T08:00:00+08:00", planned_duration_minutes: 40, max_depth_m: 18 };
const ID = "a".repeat(32);
const GOOD = "0123456789abcdef0123456789abcdef";

test("a well-formed retry key is sent with a create and with a decision", async () => {
  capture();
  await createAssessment("k-12345678", body, GOOD);
  await recordDecision("k-12345678", ID, { decision: "NO-GO", rationale: "x" }, GOOD);
  assert.deepEqual(seen, [GOOD, GOOD]);
});

test("a missing or malformed retry key is left out, never sent", async () => {
  capture();
  for (const bad of [undefined, "", "short", "has space in it 12345", "x".repeat(200), "bad\r\nHeader: 1-----"]) {
    await createAssessment("k-12345678", body, bad);
  }
  assert.deepEqual(seen, [null, null, null, null, null, null]);
});
