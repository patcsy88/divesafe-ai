import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

// ADR 0017: HeroUI's @react-types/color pulls in Adobe React Spectrum for types only. It is
// installed but must never be imported, so it can never ship. This catches direct imports only;
// CI's build check covers imports through other packages.
const ROOT = fileURLToPath(new URL("..", import.meta.url));
const SELF = fileURLToPath(import.meta.url);
const BANNED = /["'`](@adobe\/|@react-spectrum\/|@spectrum-icons\/|react-transition-group|@react-types\/color)/;

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    if (["node_modules", ".next", "package-lock.json"].includes(name)) return [];
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return files(path);
    return /\.(ts|tsx|js|jsx|mjs|cjs|mts|cts|css|json)$/.test(name) && path !== SELF ? [path] : [];
  });
}

test("no file in web/ imports Adobe React Spectrum or @react-types/color", () => {
  const found = files(ROOT);
  assert.ok(found.some((p) => p.endsWith("package.json")), "the scan found no package.json");
  for (const p of found) assert.ok(!BANNED.test(readFileSync(p, "utf8")), `${p} names a banned package`);
});
