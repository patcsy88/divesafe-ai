import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const ROOT = fileURLToPath(new URL("..", import.meta.url));

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    if (["node_modules", ".next", "tests"].includes(name)) return [];
    const path = join(dir, name);
    return statSync(path).isDirectory() ? sources(path) : /\.(ts|tsx)$/.test(name) ? [path] : [];
  });
}

const files = sources(ROOT).map((p) => ({ p, text: readFileSync(p, "utf8") }));

test("no client component imports server-only modules or reads the environment", () => {
  for (const { p, text } of files) {
    if (!text.startsWith('"use client"')) continue;
    assert.ok(!/lib\/(api|auth|config|session)\.ts/.test(text), `${p} imports a server module`);
    assert.ok(!/process\.env/.test(text), `${p} reads the environment`);
  }
});

test("nothing is exposed to the browser through NEXT_PUBLIC", () => {
  for (const { p, text } of files) assert.ok(!text.includes("NEXT_PUBLIC"), p);
});

test("the UI contains no risk logic and never talks to a model provider", () => {
  const forbidden = [/ollama/i, /openai/i, /anthropic/i, /11434/, /DIVESAFE_LLM/, /DIVESAFE_ALLOW_EXTERNAL/];
  for (const { p, text } of files) for (const re of forbidden) assert.ok(!re.test(text), `${p} matches ${re}`);
  const threshold = /(wave|wind|current|swell|visib)\w*\s*[<>]=?\s*\d/i;
  for (const { p, text } of files) assert.ok(!threshold.test(text), `${p} looks like a safety limit`);
});

test("the API key is never rendered or logged", () => {
  for (const { p, text } of files) {
    assert.ok(!/console\.(log|info|warn|error)/.test(text), `${p} logs`);
    assert.ok(!/dangerouslySetInnerHTML/.test(text), `${p} injects HTML`);
  }
});
