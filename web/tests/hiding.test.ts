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

// A link could lead out of web/ or back into itself, so none is followed.
function walk(dir: string, skip: readonly string[] = []): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const path = join(dir, e.name);
    if (skip.includes(e.name)) return [];
    assert.ok(!e.isSymbolicLink(), `${path} is a symbolic link`);
    return e.isDirectory() ? walk(path, skip) : [path];
  });
}

function source(dir: string): string[] {
  return walk(join(ROOT, dir)).filter((f) => /\.[cm]?[jt]sx?$/.test(f));
}

// Every aria-label in the app, by file. A label replaces what a screen reader announces, so on
// the Confirm button it could name an outcome other than the one sent; a new one is reviewed.
const LABELS = [
  ["components/AssessmentForm.tsx", "Time zone"],
  ["components/DecisionForm.tsx", "Confirm your decision"],
  ["components/WithheldBox.ts", "Withheld item"],
];

// Comments are not code. A comment is dropped only where it cannot be inside a string: a block
// comment starts its line, and a line comment starts a line or follows a space with no quote left
// open before it. Anything else that looks like a comment fails, rather than hide code.
function code(file: string, text: string): string {
  const lines = text.replace(/\/\*[\s\S]*?\*\//g, (block, at: number) => {
    const before = text.slice(text.lastIndexOf("\n", at - 1) + 1, at);
    assert.match(before, /^\s*$/, `${file}: a block comment that does not start its line`);
    return block.replace(/[^\n]/g, "");
  });
  return lines.replace(/^(.*?)(^|\s)\/\/.*$/gm, (_, before: string, gap: string) => {
    const open = ['"', "'", "`"].some((q) => (before.split(q).length - 1) % 2 === 1);
    assert.ok(!open, `${file}: a "//" that may be inside a string`);
    return before + gap;
  });
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
  const outside = walk(ROOT, ["node_modules", ".next", "tests", "app", "components"]);
  const jsxElsewhere = outside.filter((f) => /\.[cm]?[jt]sx$/.test(f));
  assert.deepEqual(jsxElsewhere, [], "a JSX file outside app/ and components/ is not checked");
  const labels: string[][] = [];
  for (const file of files) {
    const text = read(file);
    // A JSX attribute, with or without a value: `aria-hidden="true"`, `inert`, `hidden={x}`.
    if (file.endsWith("x")) {
      const jsx = new RegExp(`\\s(?:${ATTR})(?=\\s*=|\\s*/?>|\\s+[\\w-]+[\\s=/>])`, "i");
      assert.doesNotMatch(text, jsx, `${file}: a hiding attribute in JSX (${REWORD})`);
      assert.doesNotMatch(text, /\{\s*\.\.\./, `${file}: a spread, which could carry one`);
    }
    // Elements the browser keeps closed or never shows, and inline styles, which could hide text.
    const closed = /<(?:dialog|template)\b|\bh\(\s*["'](?:dialog|template)["']/;
    assert.doesNotMatch(text, closed, `${file}: a dialog or template`);
    assert.doesNotMatch(text, /\bstyle\s*[=:]|\.style\b|cssText/, `${file}: an inline style`);
    const label = /aria-label(?:["']\s*:\s*|=)(?:"([^"]*)"|'([^']*)'|(.))/g;
    for (const m of text.matchAll(label)) {
      assert.equal(m[3], undefined, `${file}: an aria-label that is not a literal`);
      labels.push([file.slice(ROOT.length + 1), m[1] ?? m[2] ?? ""]);
    }
    // A props key, for createElement: `"aria-hidden": true`, `inert: true`, `hidden: x`, or a
    // computed key holding one of the names.
    const key = new RegExp(`(?:^|[{,\\s])["'\`]?(?:${ATTR})["'\`]?\\s*:(?!:)`, "im");
    assert.doesNotMatch(text, key, `${file}: a hiding attribute as a props key (${REWORD})`);
    const computed = new RegExp(`\\[\\s*["'\`](?:${ATTR})`, "i");
    assert.doesNotMatch(text, computed, `${file}: a computed key`);
    // A file that builds elements (`createElement`, imported as `h`) spreads only into an array,
    // never into props, where a spread could carry one of the names.
    if (/\bcreateElement\b/.test(text)) {
      assert.doesNotMatch(text, /(?<!\[)\.\.\./, `${file}: a spread outside an array literal`);
      const alias = /import \{[^}]*\bcreateElement as (\w+)/.exec(text)?.[1];
      assert.equal(alias, "h", `${file}: createElement is imported other than as h`);
    }
    // Shorthand keys (`{ hidden }`) have no colon, so each props literal is read on its own. One
    // with braces inside it cannot be read whole here, so it fails rather than pass unread.
    const literal = /(?<![\w.])(?:h|createElement)\s*\(\s*(?:"[\w-]+"|[A-Z]\w*)\s*,\s*\{/g;
    const short = new RegExp(`(?:^|,)\\s*(?:${ATTR})\\s*(?:,|$)`, "i");
    for (const m of text.matchAll(literal)) {
      const body = /^([^{}]*)\}/.exec(text.slice((m.index ?? 0) + m[0].length));
      assert.ok(body, `${file}: props with braces inside, which this check cannot read: ${m[0]}`);
      assert.doesNotMatch(body[1] ?? "", short, `${file}: a shorthand hiding prop`);
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
  assert.deepEqual(labels.sort(), LABELS, "the aria-labels changed: each needs review (ADR 0017)");
});

test("no code in app/, components/ or lib/ sets a hiding attribute on the page", () => {
  const files = [...source("app"), ...source("components"), ...source("lib")];
  for (const file of files) {
    const text = read(file);
    const dom = /setAttribute|toggleAttribute|innerHTML|outerHTML|insertAdjacentHTML/;
    assert.doesNotMatch(text, dom, `${file}: writes attributes or HTML directly`);
    assert.doesNotMatch(text, /dangerouslySetInnerHTML/, `${file}: raw HTML`);
    const prop = new RegExp(`\\.(?:${ATTR})\\s*(?:=(?!=)|\\|\\|=|\\?\\?=|&&=)`, "i");
    assert.doesNotMatch(text, prop, `${file}: assigns a hiding property`);
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
    assert.ok(!file.endsWith("x"), `${file} is a JSX file`);
  }
});
