import assert from "node:assert/strict";
import { test } from "node:test";
import { seal, unseal } from "../lib/session.ts";

const SECRET = "s".repeat(40);
const NOW = 1_000_000;

test("a sealed key round-trips and is not readable in the token", () => {
  const token = seal("my-api-key-123", SECRET, NOW, 60);
  assert.ok(!token.includes("my-api-key"));
  assert.ok(!Buffer.from(token, "base64url").toString("utf8").includes("my-api-key"));
  assert.equal(unseal(token, SECRET, NOW + 1000), "my-api-key-123");
});

test("an expired token is refused", () => {
  const token = seal("k-12345678", SECRET, NOW, 60);
  assert.equal(unseal(token, SECRET, NOW + 61_000), null);
  assert.equal(unseal(token, SECRET, NOW + 60_000), null);
});

test("a different secret is refused", () => {
  assert.equal(unseal(seal("k-12345678", SECRET, NOW, 60), "t".repeat(40), NOW), null);
});

test("a tampered token is refused at every byte", () => {
  const raw = Buffer.from(seal("k-12345678", SECRET, NOW, 60), "base64url");
  for (let i = 0; i < raw.length; i++) {
    const copy = Buffer.from(raw);
    copy[i] = copy[i]! ^ 1;
    assert.equal(unseal(copy.toString("base64url"), SECRET, NOW), null, `byte ${i}`);
  }
});

test("garbage and truncated tokens are refused, not thrown", () => {
  for (const bad of ["", "abc", "%%%", "A".repeat(10), "A".repeat(5000)]) {
    assert.equal(unseal(bad, SECRET, NOW), null);
  }
});

test("the same key sealed twice gives different tokens", () => {
  assert.notEqual(seal("k-12345678", SECRET, NOW, 60), seal("k-12345678", SECRET, NOW, 60));
});
