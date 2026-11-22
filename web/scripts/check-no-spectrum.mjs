// ADR 0017: Adobe React Spectrum is installed (types only, through @react-types/color) but must
// never ship. Run after `next build`. Fails if a trace file or a source map names one of its
// files, or if a built file holds one of its strings. Next writes server source maps on every
// build; browser ones only with `productionBrowserSourceMaps` (scratch builds).
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const OUT = ".next";
const PATH = /node_modules\/(@adobe|@react-spectrum|@spectrum-icons|react-transition-group)\//;
// Literals that survive minification (ADR 0017). The first two are @adobe/react-spectrum's, which
// every @spectrum-icons icon and @react-spectrum package loads; the last is react-transition-group's.
const STRINGS = ["spectrum-FocusRing", "spectrum-Icon", "addEndListener"];

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

if (!existsSync(OUT)) {
  console.error("check-no-spectrum: no .next directory; run `next build` first");
  process.exit(1);
}
const all = walk(OUT);
const problems = [];
for (const p of all) if (PATH.test(p)) problems.push(`${p} is a Spectrum file`);
const traces = all.filter((p) => p.endsWith(".nft.json"));
let tracedModules = 0;
for (const p of traces) {
  for (const f of JSON.parse(readFileSync(p, "utf8")).files) {
    if (f.includes("node_modules/")) tracedModules += 1;
    if (PATH.test(f)) problems.push(`${p} traces ${f}`);
  }
}
const maps = all.filter((p) => p.endsWith(".map"));
for (const p of maps) {
  for (const s of JSON.parse(readFileSync(p, "utf8")).sources ?? []) {
    if (PATH.test(s)) problems.push(`${p} has source ${s}`);
  }
}
const browser = all.filter((p) => p.startsWith(join(OUT, "static")) && /\.(js|css)$/.test(p));
const built = all.filter((p) => /\.(js|css)$/.test(p) && !p.endsWith(".nft.json"));
for (const p of built) {
  const text = readFileSync(p, "utf8");
  for (const s of STRINGS) if (text.includes(s)) problems.push(`${p} contains "${s}"`);
}

const serverMaps = maps.filter((p) => p.startsWith(join(OUT, "server"))).length;
const counts = `${traces.length} traces, ${maps.length} maps (${serverMaps} server), ${browser.length} browser files`;
if (traces.length === 0 || tracedModules === 0 || serverMaps === 0 || browser.length === 0) {
  console.error(`check-no-spectrum: nothing to check (${counts})`);
  process.exit(1);
}
if (problems.length > 0) {
  console.error(`check-no-spectrum: Spectrum code in the build:\n${problems.join("\n")}`);
  process.exit(1);
}
console.log(`check-no-spectrum: ok (${counts}, ${built.length} built files)`);
