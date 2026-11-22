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
    const unchecked = /["']className["']\s*:|\bclass\s*[:=]/;
    assert.doesNotMatch(text, unchecked, `${file}: a class is set in a form not checked`);
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

const DARK = "@media (prefers-color-scheme: dark) { :root {";
// HeroUI's own colours fail contrast against ours (button text 3.59:1, a dark field 1.09:1
// against our card, with no border), so these HeroUI variables are set from our palette in
// both modes (option A1, owner, 2026-11-22). Every other HeroUI dark value is HeroUI's own.
const OURS = "/* HeroUI's variables, from our palette";
const FROM_PALETTE = new Map([
  ["--foreground", "var(--ds-fg)"],
  ["--background", "var(--ds-bg)"],
  ["--border", "var(--ds-line)"],
  ["--accent", "var(--ds-accent)"],
  ["--accent-foreground", "var(--ds-on-accent)"],
  ["--focus", "var(--ds-accent)"],
  ["--field-background", "var(--ds-bg)"],
  ["--field-foreground", "var(--ds-fg)"],
  ["--field-border", "var(--ds-line)"],
  ["--field-border-width", "1px"],
]);

function declarations(text: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const m of text.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/(--[\w-]+)\s*:\s*([^;}]+)/g)) {
    out.set(m[1] ?? "", (m[2] ?? "").replace(/\s+/g, " ").trim());
  }
  return out;
}

function block(text: string, opener: string): string {
  const start = text.indexOf(opener);
  assert.ok(start >= 0, `did not find ${JSON.stringify(opener)}`);
  let i = text.indexOf("{", start + opener.length - 1) + 1;
  const from = i;
  for (let depth = 1; depth > 0; i++) {
    assert.ok(i < text.length, `unbalanced braces after ${JSON.stringify(opener)}`);
    if (text[i] === "{") depth++;
    if (text[i] === "}") depth--;
  }
  return text.slice(from, i - 1);
}

test("our custom properties carry the ds- prefix and none is also a HeroUI one", () => {
  const files = walk(HEROUI).filter((f) => f.endsWith(".css"));
  const heroui = new Set<string>();
  for (const f of files) for (const name of declarations(readFileSync(f, "utf8")).keys()) {
    heroui.add(name);
  }
  for (const known of ["--accent", "--background", "--border", "--foreground", "--surface"]) {
    assert.ok(heroui.has(known), `the scan did not find HeroUI's ${known}`);
  }
  for (const name of heroui) assert.ok(!name.startsWith("--ds-"), `HeroUI defines ${name}`);
  const css = readFileSync(join(ROOT, "app/globals.css"), "utf8");
  const dark = new Set(declarations(block(css, DARK)).keys());
  const mapped = block(css.slice(css.indexOf(OURS)), ":root {");
  assert.deepEqual(declarations(mapped), FROM_PALETTE, "HeroUI variables set from our palette");
  const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");
  for (const name of declarations(css).keys()) {
    if (name.startsWith("--ds-")) {
      assert.ok(!heroui.has(name), `HeroUI also defines ${name}`);
      continue;
    }
    assert.ok(heroui.has(name), `globals.css sets ${name}, which HeroUI does not define`);
    assert.ok(dark.has(name) !== FROM_PALETTE.has(name), `globals.css sets ${name} unchecked`);
    const times = [...bare.matchAll(new RegExp(`(?<![\\w-])${name}\\s*:`, "g"))].length;
    assert.equal(times, 1, `globals.css sets ${name} more than once`);
  }
  const outside = bare.replace(block(bare, DARK), "");
  for (const m of outside.matchAll(/var\(\s*(--[\w-]+)/g)) {
    assert.ok(m[1]?.startsWith("--ds-"), `globals.css reads ${m[1]} outside the dark block`);
  }
});

test("dark mode follows the system setting with exactly HeroUI's own dark values", () => {
  const theme = readFileSync(join(HEROUI, "themes/default/variables.css"), "utf8");
  const light = declarations(block(theme, "  :root,\n  .light,"));
  const heroDark = declarations(block(theme, "  .dark,\n"));
  assert.ok(heroDark.size > 50, `found only ${heroDark.size} dark values`);
  const differ = new Map([...heroDark].filter(([name, value]) => light.get(name) !== value));
  assert.ok(differ.has("--background") && differ.has("--surface"), "no dark colours");
  const expected = new Map([...differ].filter(([name]) => !FROM_PALETTE.has(name)));
  const css = readFileSync(join(ROOT, "app/globals.css"), "utf8");
  const body = block(css, DARK);
  const ours = declarations(body);
  const count = [...body.matchAll(/--[\w-]+\s*:/g)].length;
  assert.equal(count, ours.size, "the dark block sets a property twice");
  const heroOnly = new Map([...ours].filter(([name]) => !name.startsWith("--ds-")));
  assert.deepEqual(heroOnly, expected);
});

test("every variable globals.css reads is defined, by us or by HeroUI", () => {
  const heroui = new Set<string>();
  for (const f of walk(HEROUI).filter((f) => f.endsWith(".css"))) {
    for (const name of declarations(readFileSync(f, "utf8")).keys()) heroui.add(name);
  }
  const raw = readFileSync(join(ROOT, "app/globals.css"), "utf8");
  const css = raw.replace(/\/\*[\s\S]*?\*\//g, "");
  const ours = new Set([...declarations(css).keys()].filter((n) => n.startsWith("--ds-")));
  const read = [...css.matchAll(/var\(\s*(--[\w-]+)/g)].map((m) => m[1] ?? "");
  assert.ok(read.length > 20, `found only ${read.length} var() reads`);
  for (const name of read) {
    assert.ok(ours.has(name) || heroui.has(name), `globals.css reads ${name}; nothing sets it`);
  }
});

// Copying HeroUI's dark variables covers dark mode only while HeroUI keys nothing else on
// `.dark` or `data-theme`. These are every such selector in @heroui/styles 3.2.7, read from its
// source: the light and dark theme blocks, the vibrant palette (an attribute the app never sets)
// and the `dark` variant. A HeroUI release that adds one fails here until it is reviewed. The
// variant's system fallback needs the class on an ancestor as well, so a `dark:` utility never
// applies in system dark mode (Chromium, 2026-11-22): dark styles go in globals.css instead.
const THEME = "themes/default/variables.css";
const VARIANTS = "variants/index.css";
const DARK_SELECTORS = [
  [
    THEME,
    ':root, .light, .default, [data-theme="light"], [data-theme="default"], :host(.light), ' +
      ':host(.default), :host([data-theme="light"]), :host([data-theme="default"])',
  ],
  [THEME, '.dark, [data-theme="dark"], :host(.dark), :host([data-theme="dark"])'],
  [THEME, '[data-vibrant-palette="true"]:not(.dark):not([data-theme="dark"])'],
  [THEME, '[data-vibrant-palette="true"].dark, [data-vibrant-palette="true"][data-theme="dark"]'],
  [VARIANTS, "@custom-variant dark"],
  [VARIANTS, '&:is(.dark, .dark *, [data-theme="dark"], [data-theme="dark"] *)'],
  [VARIANTS, "@media (prefers-color-scheme: dark)"],
  [VARIANTS, '&:not(:is(.dark, .dark *, [data-theme="dark"], [data-theme="dark"] *)) &'],
];

test("HeroUI keys nothing on .dark or data-theme but its theme variables and dark variant", () => {
  const found: string[][] = [];
  for (const f of walk(HEROUI).filter((f) => f.endsWith(".css") && !f.endsWith(".min.css"))) {
    const text = readFileSync(f, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    for (const m of text.matchAll(/([^{};]*)\{/g)) {
      const selector = (m[1] ?? "").replace(/\s+/g, " ").trim();
      if (/\.dark\b|data-theme|\bdark\b|data-vibrant/.test(selector)) {
        found.push([f.slice(HEROUI.length + 1), selector]);
      }
    }
  }
  assert.deepEqual(found, DARK_SELECTORS);
  const react = join(HEROUI, "../../react/dist");
  const code = [...walk(HEROUI), ...walk(react)].filter((f) => /\.(css|js)$/.test(f));
  assert.ok(code.length > 100, `found only ${code.length} HeroUI files`);
  for (const f of code) {
    const text = readFileSync(f, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    assert.doesNotMatch(text, /(?<![\w-])dark:/, `${f} uses a dark: utility`);
  }
  const pkg = JSON.parse(readFileSync(join(HEROUI, "../package.json"), "utf8"));
  assert.equal(pkg.style, "./dist/index.css", "@heroui/styles ships another stylesheet");
  assert.equal(pkg.exports?.["."]?.style, "./dist/index.css", "@heroui/styles exports another");
  const theme = readFileSync(join(HEROUI, THEME), "utf8");
  const other = block(theme, "  .dark,\n")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split(";")
    .map((d) => d.replace(/\s+/g, " ").trim())
    .filter((d) => d && !d.startsWith("--"));
  assert.deepEqual(other, ["color-scheme: dark"], "HeroUI's .dark sets more than variables");
  const root = readFileSync(join(ROOT, "app/globals.css"), "utf8");
  assert.match(root, /^:root \{ color-scheme: light dark;/m, "the page no longer follows dark");
  const dirs = ["app", "components", "lib"].map((d) => join(ROOT, d));
  const files = dirs.flatMap(walk).filter((f) => /\.(tsx?|jsx?|mjs|css)$/.test(f));
  assert.ok(files.length > 20, `found only ${files.length} app files`);
  for (const file of files) {
    const text = readFileSync(file, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    const themes = /data-theme|data-vibrant|["'`]dark["'`]|\.dark\b|(?<![\w-])dark:/;
    assert.doesNotMatch(text, themes, `${file} sets a theme or uses a dark: utility`);
    const kept = /localStorage|sessionStorage|document\.cookie|next-themes|ThemeProvider|useTheme/;
    assert.doesNotMatch(text, kept, `${file} stores or provides a theme`);
  }
});

// HeroUI components go only in client components, by subpath import, from this list. Props
// that change visible text or hide it are refused: `isRequired` adds a "*" to the label,
// `isPending` swaps the button's type, and `Description` renders nothing outside a field. React
// Aria's own packages are not imported directly, so the same list covers them.
const COMPONENTS = new Map([
  ["@heroui/react/button", "Button"],
  ["@heroui/react/input", "Input"],
  ["@heroui/react/label", "Label"],
  ["@heroui/react/textarea", "TextArea"],
]);

test("HeroUI components are imported by subpath, from a fixed list, in client files only", () => {
  const files = ["app", "components", "lib"]
    .flatMap((d) => walk(join(ROOT, d)))
    .filter((f) => /\.(tsx?|jsx?|mjs)$/.test(f));
  let uses = 0;
  for (const file of files) {
    const text = readFileSync(file, "utf8");
    const before = uses;
    const pkg = /(?:@heroui\/|react-aria|@react-(?:aria|stately)\/)/.source;
    const named = new RegExp(`(?:from|import|require)\\s*\\(?\\s*["'\`]${pkg}`, "g");
    for (const m of text.matchAll(named)) {
      const end = text.indexOf("\n", m.index);
      const line = text.slice(text.lastIndexOf("\n", m.index) + 1, end < 0 ? undefined : end);
      const listed = line.match(/^import \{ (\w+) \} from "(@heroui\/react\/[\w-]+)";$/);
      assert.ok(listed && COMPONENTS.get(listed[2] ?? "") === listed[1], `${file}: ${line}`);
      uses++;
      assert.ok(text.startsWith('"use client"'), `${file} uses HeroUI outside a client file`);
    }
    const mentions = text.match(new RegExp(pkg, "g"))?.length ?? 0;
    assert.equal(mentions, uses - before, `${file} names HeroUI or React Aria outside an import`);
    assert.doesNotMatch(text, /\bisRequired\b|\bisPending\b|<Description\b/, file);
    // Text in fields stays 16px: smaller hides a mistyped depth, and iOS zooms in below 16px.
    for (const input of text.matchAll(/<(?:Input|TextArea)\b[^>]*>/g)) {
      const field = /\sclassName="ds-field"\s/;
      assert.match(input[0], field, `${file}: an Input or TextArea without ds-field`);
      assert.doesNotMatch(input[0], /\{\s*\.\.\./, `${file}: a spread on an Input or TextArea`);
    }
    assert.doesNotMatch(text, /(?:createElement|\bh)\(\s*(?:Input|TextArea|Button|Label)\b/, file);
  }
  assert.ok(uses >= 6, `found only ${uses} HeroUI imports`);
  const css = readFileSync(join(ROOT, "app/globals.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  let rules = 0;
  for (const m of css.matchAll(/(?:^|[{},])\s*((?:button|input|label|textarea)\b[^,{]*)/g)) {
    rules++;
    assert.match(m[1] ?? "", /^\w+:where\(:not\(\[data-slot\]\)\)/, `${m[1]} also styles HeroUI`);
  }
  assert.ok(rules >= 7, `found only ${rules} element rules`);
});
