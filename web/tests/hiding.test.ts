import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// `aria-hidden`, `inert` and `hidden` take text out of what a screen reader announces or off the
// page. On the PENDING HUMAN banner, the badge or the DEGRADED note that would hide a safety
// sentence, so the app's own code never sets them (ADR 0008 Amendment 1 (a)). React Aria passes
// an `inert` prop through to the DOM, so a HeroUI component is no exception. A form field's
// `type="hidden"` is a value, not one of these attributes, and is allowed. `popover`, a closed
// `<dialog>`, `<template>` and inline styles hide text too, and are refused with them. This reads
// the source as text, not as a parsed program: a tripwire, not proof (a name built at run time
// passes).
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const ATTR = /aria-?hidden|inert|hidden|popover/i.source;

// A link could lead out of web/ or back into itself, so none is followed. `skip` names folders
// directly under web/ only, so an `app/` or `tests/` folder deeper down is still walked.
function walk(dir: string, skip: readonly string[] = []): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const path = join(dir, e.name);
    if (dir === ROOT && skip.includes(e.name)) return [];
    assert.ok(!e.isSymbolicLink(), `${path} is a symbolic link`);
    return e.isDirectory() ? walk(path, skip) : [path];
  });
}

function source(dir: string): string[] {
  return walk(join(ROOT, dir)).filter((f) => /\.[cm]?[jt]sx?$/.test(f));
}

// Script files under web/ that none of these checks reads: build and CI configuration, each
// reviewed. Anything else outside app/, components/, lib/ and tests/ fails, so no new folder of
// code (hooks/, src/, ui/) escapes the checks. Next writes `next-env.d.ts` (types only, ignored).
const UNSCANNED = [
  "next.config.ts",
  "postcss.config.mjs",
  "scripts/check-built-css.mjs",
  "scripts/check-no-spectrum.mjs",
];

// Every aria-label and aria-labelledby in the app, by file. A label replaces what a screen reader
// announces, so on the Confirm button it could name an outcome other than the one sent; a new one
// is reviewed.
const LABELS = [
  ["components/AssessmentForm.tsx", "aria-label", "Time zone"],
  ["components/DecisionForm.tsx", "aria-label", "Confirm your decision"],
  ["components/ProviderNotices.ts", "aria-labelledby", "provider-notice-heading"],
  ["components/WithheldBox.ts", "aria-label", "Withheld item"],
  ["components/WithheldBox.ts", "aria-labelledby", "withheld-heading"],
];

// Every <details> in the app, by file: it starts closed, so what it holds is off the page until
// opened, and no safety sentence moves into one without review.
const DETAILS = [
  ["app/assessments/[id]/evidence/page.tsx", 1],
  ["components/WithheldBox.ts", 1],
];

// Comments are not code. Only a comment that starts its line is dropped: a block comment that
// does not fails, and a trailing `//` comment is kept and read as code (move it to its own line
// if a check trips on it). So nothing inside a string or JSX text is ever dropped.
function code(file: string, text: string): string {
  const lines = text.replace(/\/\*[\s\S]*?\*\//g, (block, at: number) => {
    const before = text.slice(text.lastIndexOf("\n", at - 1) + 1, at);
    assert.match(before, /^\s*$/, `${file}: a block comment that does not start its line`);
    return block.replace(/[^\n]/g, "");
  });
  return lines.replace(/^\s*\/\/.*$/gm, "");
}
const read = (file: string) => code(file, readFileSync(file, "utf8"));

// A failure on text that is not an attribute is fixed by rewording the text, never by loosening
// a pattern here (ADR 0017).
const REWORD = "if this is text, not an attribute, reword it; do not loosen the pattern";

test("no element in app/ or components/ is hidden, closed or relabelled by our code", () => {
  const files = [...source("app"), ...source("components")];
  assert.ok(files.length > 15, `found only ${files.length} files`);
  const other = files.filter((f) => !/\.tsx?$/.test(f));
  assert.deepEqual(other, [], "app/ and components/ hold only .ts and .tsx files");
  const outside = walk(ROOT, ["node_modules", ".next", "tests", "app", "components", "lib"])
    .filter((f) => /\.[cm]?[jt]sx?$/.test(f))
    .map((f) => f.slice(ROOT.length + 1))
    .filter((f) => f !== "next-env.d.ts")
    .sort();
  assert.deepEqual(outside, UNSCANNED, "a script outside the checked folders needs review");
  const labels: string[][] = [];
  const details: (string | number)[][] = [];
  for (const file of files) {
    const text = read(file);
    const name = file.slice(ROOT.length + 1);
    // An escaped name (`in\u0065rt`) would pass every pattern here.
    assert.doesNotMatch(text, /\\u/, `${file}: a \\u escape, which these checks cannot read`);
    // A JSX attribute, with or without a value: `aria-hidden="true"`, `inert`, `hidden={x}`.
    if (file.endsWith("x")) {
      const jsx = new RegExp(`\\s(?:${ATTR})(?=\\s*=|\\s*/?>|\\s+[\\w-]+[\\s=/>])`, "i");
      assert.doesNotMatch(text, jsx, `${file}: a hiding attribute in JSX (${REWORD})`);
      assert.doesNotMatch(text, /\{\s*\.\.\./, `${file}: a spread, which could carry one`);
    }
    // Elements the browser keeps closed or never shows, and inline styles, which could hide text.
    const closed = /<(?:dialog|template)\b|\b(?:h|createElement)\(\s*["'`](?:dialog|template)\b/;
    assert.doesNotMatch(text, closed, `${file}: a dialog or template`);
    const style = /\bstyle["'`]?\s*[=:]|\.style\b|cssText/;
    assert.doesNotMatch(text, style, `${file}: an inline style`);
    // A role of none or presentation removes what a screen reader announces for an element.
    const role = /\brole["'`]?\s*[=:]\s*\{?\s*["'`](?:none|presentation)/;
    assert.doesNotMatch(text, role, `${file}: a role that removes the element's meaning`);
    const opened = /<details\b|\b(?:h|createElement)\(\s*["'`]details\b/g;
    const n = text.match(opened)?.length ?? 0;
    if (n > 0) details.push([name, n]);
    const label = /(aria-label(?:ledby)?)(?:["'`]\s*:|\s*=)\s*(?:"([^"]*)"|'([^']*)'|(.))/g;
    for (const m of text.matchAll(label)) {
      assert.equal(m[4], undefined, `${file}: an ${m[1]} that is not a literal`);
      labels.push([name, m[1] ?? "", m[2] ?? m[3] ?? ""]);
    }
    assert.equal(
      text.match(/aria-label/g)?.length ?? 0,
      labels.filter(([f]) => f === name).length,
      `${file}: an aria-label in a form not read here`,
    );
    // A props key, for createElement: `"aria-hidden": true`, `inert: true`, `hidden: x`, or a
    // computed key holding one of the names.
    const key = new RegExp(`(?:^|[{,\\s])["'\`]?(?:${ATTR})["'\`]?\\s*:(?!:)`, "im");
    assert.doesNotMatch(text, key, `${file}: a hiding attribute as a props key (${REWORD})`);
    const computed = new RegExp(`\\[\\s*["'\`](?:${ATTR})`, "i");
    assert.doesNotMatch(text, computed, `${file}: a computed key`);
    // A file that builds elements (`createElement`, imported as `h`) spreads only into an array,
    // never into props, where a spread could carry one of the names.
    // `createElement` appears once, in `import { createElement as h }`, and `h` is only ever
    // called: never passed on, renamed, or used through `.call` or `.apply`.
    if (/\bcreateElement\b/.test(text)) {
      assert.doesNotMatch(text, /(?<!\[)\.\.\./, `${file}: a spread outside an array literal`);
      const once = text.match(/\bcreateElement\b/g)?.length ?? 0;
      assert.equal(once, 1, `${file}: createElement named more than once`);
      assert.match(text, /import \{[^}]*\bcreateElement as h\b/, `${file}: not imported as h`);
      const rest = text.replace(/\bcreateElement as h\b/, "");
      assert.doesNotMatch(rest, /(?<![\w.$])h\b(?!\s*\()/, `${file}: h used other than as a call`);
    }
    // Shorthand keys (`{ hidden }`) have no colon, so each props literal is read on its own. One
    // with braces, a computed key or a quote left open inside cannot be read whole here, so it
    // fails rather than pass unread.
    const literal = /(?<![\w.])(?:h|createElement)\s*\(\s*(?:"[\w-]+"|[A-Z]\w*)\s*,\s*\{/g;
    const short = new RegExp(`(?:^|,)\\s*(?:${ATTR})\\s*(?:,|$)`, "i");
    for (const m of text.matchAll(literal)) {
      const body = /^([^{}]*)\}/.exec(text.slice((m.index ?? 0) + m[0].length));
      assert.ok(body, `${file}: props with braces inside, which this check cannot read: ${m[0]}`);
      const props = body[1] ?? "";
      assert.doesNotMatch(props, /\[/, `${file}: a computed props key, which this cannot read`);
      for (const q of ['"', "'", "`"]) {
        const even = (props.split(q).length - 1) % 2 === 0;
        assert.ok(even, `${file}: a brace inside a string in props, which this cannot read`);
      }
      assert.doesNotMatch(props, short, `${file}: a shorthand hiding prop`);
    }
    // Every props argument is written out here, as null or an object literal, so the key check
    // above reads every key: props built elsewhere (in lib/, say) never reach an element.
    const direct = /\b(?:cloneElement|jsxs?|jsxDEV)\s*\(|react\/jsx-(?:dev-)?runtime/;
    assert.doesNotMatch(text, direct, `${file}: builds elements directly`);
    const calls = text.match(/(?<![\w.])(?:h|createElement)\s*\(/g)?.length ?? 0;
    const first = /(?<![\w.])(?:h|createElement)\s*\(\s*(?:"[\w-]+"|[A-Z]\w*)\s*,\s*(?:null\b|\{)/g;
    const literals = text.match(first)?.length ?? 0;
    assert.equal(literals, calls, `${file}: props that are not null or a literal`);
  }
  const order = (a: string[], b: string[]) => (a.join("\0") < b.join("\0") ? -1 : 1);
  const changed = "the aria-labels changed: each needs review (ADR 0017)";
  assert.deepEqual(labels.sort(order), [...LABELS].sort(order), changed);
  assert.deepEqual(details.sort(), DETAILS, "the <details> changed: each needs review (ADR 0017)");
});

test("no code in app/, components/ or lib/ sets a hiding attribute on the page", () => {
  const files = [...source("app"), ...source("components"), ...source("lib")];
  for (const file of files) {
    const text = read(file);
    const dom = /setAttribute|toggleAttribute|innerHTML|outerHTML|insertAdjacentHTML/;
    assert.doesNotMatch(text, dom, `${file}: writes attributes or HTML directly`);
    assert.doesNotMatch(text, /dangerouslySetInnerHTML/, `${file}: raw HTML`);
    const assign = /\s*(?:=(?!=)|\|\|=|\?\?=|&&=)/.source;
    const names = `(?:${ATTR}|ariaLabel(?:ledBy)?)`;
    const prop = new RegExp(`\\.${names}${assign}`, "i");
    assert.doesNotMatch(text, prop, `${file}: assigns a hiding property`);
    const bracket = new RegExp(`\\[\\s*["'\`]${names}["'\`]\\s*\\]${assign}`, "i");
    assert.doesNotMatch(text, bracket, `${file}: assigns a hiding property by name`);
    const indirect = /Object\.assign|Reflect\.set|defineProperty/;
    assert.doesNotMatch(text, indirect, `${file}: sets properties indirectly`);
    // Classes: classes.test.ts allows only ds- classes styled in globals.css, in className
    // literals; the DOM routes around it are refused here.
    assert.doesNotMatch(text, /classList|\.className\s*=/, `${file}: sets classes on the page`);
    // Removing text from the page, or closing a <details>.
    const remove =
      /textContent\s*=|innerText\s*=|replaceChildren|removeChild|\.remove\(\)|\.open\s*=/;
    assert.doesNotMatch(text, remove, `${file}: removes text from the page`);
  }
});

// lib/ renders nothing; and since every props argument is a literal in the file that renders it,
// a lib/ object never becomes props.
test("lib/ renders nothing", () => {
  assert.ok(source("lib").length > 5, "found almost no files in lib/");
  for (const file of source("lib")) {
    const text = read(file);
    const react = /(?:from\s+|(?:require|import)\s*\(\s*)["']react(?:-dom)?(?:\/[\w-]+)?["']/;
    assert.doesNotMatch(text, react, `${file} imports React`);
    assert.doesNotMatch(text, /\bcreateElement\b|\bjsxs?\(/, `${file} builds elements`);
    const page = /\b(?:document|window)\b|querySelector|getElementBy/;
    assert.doesNotMatch(text, page, `${file} reaches the page`);
    assert.ok(!file.endsWith("x"), `${file} is a JSX file`);
  }
});
