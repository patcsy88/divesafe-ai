import assert from "node:assert/strict";
import { test } from "node:test";
import { trustProxy } from "../lib/config.ts";
import {
  bucketOf,
  forwardedAddress,
  LoginThrottle,
  MAX_FAILURES,
  sourceOf,
} from "../lib/throttle.ts";

test("the web limit trips before the API's default of 10 failures a minute", () => {
  assert.ok(MAX_FAILURES < 10);
});

test("a source is blocked after its attempts and told how long to wait", () => {
  const t = new LoginThrottle(3, 60_000);
  for (let i = 0; i < 3; i++) assert.equal(t.reserve("a", 1000 + i), null);
  const wait = t.reserve("a", 2000);
  assert.ok(wait !== null && wait >= 1 && wait <= 60);
});

test("parallel attempts cannot all pass before any is counted", async () => {
  const t = new LoginThrottle(5, 60_000);
  const results = await Promise.all(
    Array.from({ length: 50 }, async () => t.reserve("a", 1000)),
  );
  assert.equal(results.filter((r) => r === null).length, 5);
});

test("a blocked attempt does not extend its own block", () => {
  const t = new LoginThrottle(1, 60_000);
  assert.equal(t.reserve("a", 0), null);
  for (let i = 1; i < 50; i++) assert.notEqual(t.reserve("a", i * 1000), null);
  assert.equal(t.reserve("a", 60_001), null);
});

test("the block lifts when the window passes", () => {
  const t = new LoginThrottle(2, 60_000);
  t.reserve("a", 0);
  t.reserve("a", 1);
  assert.notEqual(t.reserve("a", 30_000), null);
  assert.equal(t.reserve("a", 60_001), null);
});

test("releasing gives the attempt back, and only for that source", () => {
  const t = new LoginThrottle(1, 60_000);
  assert.equal(t.reserve("a", 0), null);
  assert.notEqual(t.reserve("a", 1), null);
  t.release("a");
  assert.equal(t.reserve("a", 2), null);
  assert.equal(t.reserve("b", 2), null);
  t.release("nobody");
});

test("the table stays bounded under many sources and keeps blocked ones", () => {
  const t = new LoginThrottle(2, 60_000, 100);
  t.reserve("victim", 0);
  t.reserve("victim", 1);
  assert.notEqual(t.reserve("victim", 2), null);
  for (let i = 0; i < 2000; i++) t.reserve(`s${i}`, 2 + i);
  assert.ok((t as unknown as { hits: Map<string, number[]> }).hits.size <= 101);
  assert.notEqual(t.reserve("victim", 5000), null, "the blocked source is still blocked");
});

test("the forwarded address is ignored unless the proxy is trusted, and garbage is ignored", () => {
  assert.equal(sourceOf("1.2.3.4", false), "shared");
  assert.equal(sourceOf("1.2.3.4", true), "1.2.3.4");
  assert.equal(sourceOf("6.6.6.6, 1.2.3.4", true), "1.2.3.4");
  for (const bad of ["", "not-an-ip", "1.2.3.4:80", "1.2.3.4, evil", null]) {
    assert.equal(sourceOf(bad, true), "shared", String(bad));
  }
  assert.equal(forwardedAddress("1.2.3.4", false), null);
  assert.equal(forwardedAddress("6.6.6.6, 1.2.3.4", true), "1.2.3.4");
  assert.equal(forwardedAddress("evil", true), null);
});

test("one machine has one spelling: zone ids, mapped IPv4 and IPv6 /64", () => {
  assert.equal(bucketOf("::ffff:1.2.3.4"), "1.2.3.4");
  assert.equal(bucketOf("::ffff:102:304"), "1.2.3.4");
  assert.equal(bucketOf("0:0:0:0:0:ffff:102:304"), "1.2.3.4");
  assert.equal(bucketOf("fe80::1%eth0"), bucketOf("fe80::2"));
  assert.equal(bucketOf("2001:db8:1:2:aaaa::1"), bucketOf("2001:db8:1:2:bbbb::9"));
  assert.notEqual(bucketOf("2001:db8:1:2::1"), bucketOf("2001:db8:1:3::1"));
  assert.equal(bucketOf("2001:0db8:0001:0002:0:0:0:1"), bucketOf("2001:db8:1:2::1"));
  assert.equal(bucketOf("x".repeat(100)), null);
  assert.equal(bucketOf(""), null);
});

test("proxy trust is off unless explicitly set to 1", () => {
  assert.equal(trustProxy({}), false);
  for (const v of ["", "0", "true", "yes", "2"]) {
    assert.equal(trustProxy({ DIVESAFE_WEB_TRUST_PROXY: v }), false, v);
  }
  assert.equal(trustProxy({ DIVESAFE_WEB_TRUST_PROXY: "1" }), true);
});
