import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { ApiError } from "../lib/api.ts";

const root = fileURLToPath(new URL("..", import.meta.url));

function sources(dir: string): string[] {
  return readdirSync(join(root, dir)).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(join(root, path)).isDirectory()) return sources(path);
    return path.endsWith(".tsx") ? [path] : [];
  });
}

// Printed values that are not text an attacker can shape: fixed wording, booleans, counts, or
// values that already passed through plain() at their source.
const SAFE = [
  /^(plain|labelFor|formatWhen|provenanceLabel|shown|failedReason)\(/,
  /\.map\(plain\)\.join\(/,
  /^[\w.]+\.length\b/,
  /^[\w.?]+ \?\s*(plain\([^)]*\)|"[^"]*"|`[^`]*`)\s*:\s*(plain\([^)]*\)|"[^"]*"|`[^`]*`)$/,
  /^[\w.]+ && [(<]/,
];
const NAMED_SAFE = new Set([
  "children", "problem", "state.error", "s.label", "f.label", "system", "context", "choice",
  "error.message",
  // Destructuring and object literals, not printed text.
  "params", "id: string", "item, effective_quality, retrieval_age_minutes_at_assessment, future_dated",
  "index: false, follow: false", "id: s.site.id, label: plain(s.site.name)",
  // Fixed wording, a constant list entry, and the person's own typing.
  'c.threshold_status !== "VALIDATED" && " (a placeholder, not a limit)"',
  "r", 'r === system ? " (the system\'s recommendation)" : ""', "reason.trim()",
]);

test("every value printed by a page or component is wrapped in plain() or is known fixed text", () => {
  const unwrapped: string[] = [];
  for (const file of [...sources("app"), ...sources("components")]) {
    const text = readFileSync(join(root, file), "utf8");
    // Braces that print: not attribute values (`=`), template slots (`$`), imports or types.
    for (const m of text.matchAll(/(?<![=$\w])\{([^{}]+)\}/g)) {
      const expr = (m[1] ?? "").trim();
      const before = text.slice(0, m.index);
      const lineStart = before.lastIndexOf("\n") + 1;
      const line = text.slice(lineStart, text.indexOf("\n", m.index));
      if (/^\s*(import|export|const|let|type|interface|function|return|if|for)\b/.test(line) && !line.includes(">")) continue;
      if (!/^[A-Za-z_]/.test(expr) || /[;]|=>/.test(expr) && !/^\w+\s*&&/.test(expr)) continue;
      if (SAFE.some((re) => re.test(expr)) || NAMED_SAFE.has(expr)) continue;
      if (/^[\w.]+ \? .*plain\(/.test(expr)) continue;
      unwrapped.push(`${file}: ${expr.slice(0, 80)}`);
    }
  }
  assert.deepEqual(unwrapped, []);
});

test("an API error message is stripped of control and bidi characters whoever built it", () => {
  assert.equal(new ApiError(500, "a\u202Eb\u0000c").message, "abc");
});
