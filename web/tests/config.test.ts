import assert from "node:assert/strict";
import { test } from "node:test";
import { apiBaseUrl, sessionSecret } from "../lib/config.ts";

test("the default is loopback http", () => {
  assert.equal(apiBaseUrl({}), "http://127.0.0.1:8000");
});

test("https is accepted for a remote host and a trailing slash is dropped", () => {
  assert.equal(apiBaseUrl({ DIVESAFE_API_URL: "https://api.example.org/" }), "https://api.example.org");
});

test("plain http is refused for a remote host", () => {
  for (const url of ["http://api.example.org", "http://10.0.0.5:8000", "http://localhost.evil.example"]) {
    assert.throws(() => apiBaseUrl({ DIVESAFE_API_URL: url }), /https/);
  }
});

test("credentials, queries, fragments and other schemes are refused", () => {
  for (const url of [
    "https://u:p@api.example.org",
    "https://api.example.org/?x=1",
    "https://api.example.org/#f",
    "ftp://localhost",
    "file:///etc/passwd",
    "not a url",
  ]) {
    assert.throws(() => apiBaseUrl({ DIVESAFE_API_URL: url }), Error, url);
  }
});

test("a missing or short session secret is refused", () => {
  assert.throws(() => sessionSecret({}), /32/);
  assert.throws(() => sessionSecret({ DIVESAFE_WEB_SESSION_SECRET: "short" }), /32/);
  assert.equal(sessionSecret({ DIVESAFE_WEB_SESSION_SECRET: "x".repeat(32) }), "x".repeat(32));
});
