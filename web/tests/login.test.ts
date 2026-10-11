import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { checkKey } from "../lib/api.ts";

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
