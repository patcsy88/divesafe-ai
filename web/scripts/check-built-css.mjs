// ADR 0017: the shipped CSS loads nothing at runtime from anywhere but this site. Run after
// `next build`. Fails if a built stylesheet keeps an @import, names an http(s) or
// protocol-relative address, or has a url() or image-set() address that is not a data: URI or a
// /_next/static/ file.
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const OUT = join(".next", "static");

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

if (!existsSync(OUT)) {
  console.error("check-built-css: no .next/static directory; run `next build` first");
  process.exit(1);
}
const sheets = walk(OUT).filter((p) => p.endsWith(".css"));
if (sheets.length === 0) {
  console.error("check-built-css: nothing to check (no built stylesheet)");
  process.exit(1);
}
const problems = [];
for (const p of sheets) {
  const text = readFileSync(p, "utf8");
  if (/@import/i.test(text)) problems.push(`${p} keeps an @import`);
  for (const m of text.matchAll(/https?:|["'(]\s*\/\//gi)) {
    problems.push(`${p} names an address: ${text.slice(m.index, m.index + 60)}`);
  }
  const targets = [...text.matchAll(/url\(\s*(["']?)([^"')]*)/gi)].map((m) => m[2] ?? "");
  // image-set() also takes plain strings as image addresses.
  for (const m of text.matchAll(/image-set\(([^)]*)\)/gi)) {
    for (const q of (m[1] ?? "").matchAll(/["']([^"']*)["']/g)) targets.push(q[1] ?? "");
  }
  for (const target of targets) {
    if (!/^data:/i.test(target) && !target.startsWith("/_next/static/")) {
      problems.push(`${p} loads url(${target.slice(0, 60)})`);
    }
  }
}
if (problems.length > 0) {
  console.error(`check-built-css: the built CSS loads from elsewhere:\n${problems.join("\n")}`);
  process.exit(1);
}
console.log(`check-built-css: ok (${sheets.length} stylesheets)`);
