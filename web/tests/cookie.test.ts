import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { COOKIE, COOKIE_OPTIONS } from "../lib/cookie.ts";

// Owner #139a, security review R3.
test("the session cookie is a __Host- cookie that ends with the browser", () => {
  assert.equal(COOKIE, "__Host-divesafe_session");
  assert.deepEqual({ ...COOKIE_OPTIONS }, { httpOnly: true, sameSite: "strict", secure: true, path: "/" });
  for (const absent of ["maxAge", "expires", "domain", "partitioned"]) {
    assert.ok(!(absent in COOKIE_OPTIONS), absent);
  }
});

test("auth.ts sets and clears the cookie only with these options, and nothing else names a cookie", () => {
  const auth = readFileSync(new URL("../lib/auth.ts", import.meta.url), "utf8");
  assert.match(auth, /\.set\(\s*COOKIE,\s*seal\(apiKey, sessionSecret\(\), Date\.now\(\), SESSION_MAX_AGE_SECONDS\),\s*COOKIE_OPTIONS,\s*\)/);
  assert.match(auth, /\.delete\(\{ name: COOKIE, \.\.\.COOKIE_OPTIONS \}\)/);
  assert.doesNotMatch(auth.replace(/^\s*\/\/.*$/gm, ""), /maxAge|expires|domain|secure|divesafe_session/i);
  assert.equal((auth.match(/cookies\(\)\)\.(set|delete)\(/g) ?? []).length, 2);
});

test("signing out sends a clearing cookie the browser accepts for a __Host- name", async () => {
  // The same class Next uses for cookies() in a server action or route handler.
  const { ResponseCookies } = (await import("next/dist/compiled/@edge-runtime/cookies/index.js")) as unknown as {
    ResponseCookies: new (h: Headers) => { delete(o: Record<string, unknown>): unknown };
  };
  const headers = new Headers();
  new ResponseCookies(headers).delete({ name: COOKIE, ...COOKIE_OPTIONS });
  const parts = (headers.get("set-cookie") ?? "").split(";").map((p) => p.trim());
  assert.equal(parts[0], `${COOKIE}=`);
  const attrs = parts.slice(1).map((p) => p.toLowerCase());
  assert.ok(attrs.includes("secure") && attrs.includes("path=/"), attrs.join("; "));
  assert.ok(attrs.some((a) => a.startsWith("expires=thu, 01 jan 1970")), attrs.join("; "));
  assert.ok(!attrs.some((a) => a.startsWith("domain=")), attrs.join("; "));
});
