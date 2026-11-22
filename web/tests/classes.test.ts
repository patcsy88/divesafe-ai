import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { badgeClass, RECOMMENDATIONS } from "../lib/present.ts";

// HeroUI's stylesheet applies to every page. A class of ours that HeroUI also defines takes on
// every property our rule does not set (ADR 0017), so app classes carry the `ds-` prefix and no
// HeroUI class may share a name with one of them.
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const HEROUI = join(ROOT, "node_modules/@heroui/styles/dist");

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

function appClasses(): Set<string> {
  const out = new Set<string>();
  const files = [...walk(join(ROOT, "app")), ...walk(join(ROOT, "components"))];
  for (const file of files.filter((f) => /\.tsx?$/.test(f))) {
    const text = readFileSync(file, "utf8");
    for (const m of text.matchAll(/className(?:=|:\s*)(.)/g)) {
      assert.match(m[1] ?? "", /["'`{]/, `${file}: className is not a literal`);
    }
    for (const m of text.matchAll(/className(?:=|:\s*)(["'`{])([^"'`}]*)/g)) {
      if (m[1] === "{") {
        assert.match(m[2] ?? "", /^badgeClass\(/, `${file}: a computed className is not checked`);
        continue;
      }
      for (const t of (m[2] ?? "").split(/\s+/).filter(Boolean)) out.add(t);
    }
  }
  for (const rec of [...RECOMMENDATIONS, "SAFE"]) {
    for (const t of badgeClass(rec).split(/\s+/)) out.add(t);
  }
  return out;
}

function cssClasses(text: string): Set<string> {
  // Comments and quoted strings (import paths, content) are not selectors.
  const bare = text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/"[^"]*"|'[^']*'/g, "");
  return new Set([...bare.matchAll(/(?<!\d)\.(-?[a-zA-Z_][\w-]*)/g)].map((m) => m[1] ?? ""));
}

test("every class the pages use carries the ds- prefix and is styled in globals.css", () => {
  const used = appClasses();
  assert.ok(used.size >= 20, `found only ${used.size} classes`);
  const styled = cssClasses(readFileSync(join(ROOT, "app/globals.css"), "utf8"));
  for (const c of used) {
    assert.match(c, /^ds-/, `${c} has no ds- prefix`);
    assert.ok(styled.has(c), `${c} is used but has no rule in globals.css`);
  }
  for (const c of styled) assert.match(c, /^ds-/, `globals.css styles ${c}, with no ds- prefix`);
});

test("no class of ours is also a HeroUI class, and HeroUI has no ds- class", () => {
  const files = walk(HEROUI).filter((f) => f.endsWith(".css"));
  assert.ok(files.length > 10, `found only ${files.length} HeroUI stylesheets`);
  const heroui = new Set<string>();
  for (const f of files) for (const c of cssClasses(readFileSync(f, "utf8"))) heroui.add(c);
  for (const known of ["badge", "card", "label", "link", "tag"]) {
    assert.ok(heroui.has(known), `the scan did not find HeroUI's .${known}`);
  }
  for (const c of heroui) assert.ok(!c.startsWith("ds-"), `HeroUI defines .${c}`);
  const ours = new Set([
    ...appClasses(),
    ...cssClasses(readFileSync(join(ROOT, "app/globals.css"), "utf8")),
  ]);
  const shared = [...ours].filter((c) => heroui.has(c));
  assert.deepEqual(shared, [], `HeroUI also defines: ${shared.join(" ")}`);
});
