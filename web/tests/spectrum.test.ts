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
const BANNED = /["'`(](@adobe\/|@react-spectrum\/|@spectrum-icons\/|react-transition-group|@react-types\/color)/;

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

test("the lock file keeps the Spectrum icon set at 2.3.5 and holds no -ND or -NC licence", () => {
  const lock = JSON.parse(readFileSync(join(ROOT, "package-lock.json"), "utf8")) as {
    packages: Record<string, { version?: string; license?: string }>;
  };
  // 2.4.0 and later are CC-BY-ND-4.0 (ADR 0017); a move needs the owner's acceptance.
  assert.equal(lock.packages["node_modules/@adobe/react-spectrum-workflow"]?.version, "2.3.5");
  for (const [name, entry] of Object.entries(lock.packages)) {
    if (name.endsWith("node_modules/@adobe/react-spectrum-workflow")) assert.equal(entry.version, "2.3.5", name);
    assert.ok(!/-(ND|NC)\b/i.test(entry.license ?? ""), `${name} is ${entry.license}`);
  }
});
