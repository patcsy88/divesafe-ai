// ADR 0017: the shipped CSS loads nothing at runtime from anywhere but this site. Run after
// `next build`. Fails if a built stylesheet keeps an @import, names an http(s) or
// protocol-relative address, or has a url() that is not a data: URI or a /_next/static/ file.
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
  for (const m of text.matchAll(/https?:|\/\/[a-z0-9.-]+\.[a-z]{2,}/gi)) {
    problems.push(`${p} names an address: ${text.slice(m.index, m.index + 60)}`);
  }
  for (const m of text.matchAll(/url\(\s*(["']?)([^"')]*)/gi)) {
    const target = m[2] ?? "";
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
