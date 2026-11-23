import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// `aria-hidden`, `inert` and `hidden` take text out of what a screen reader announces or off the
// page. On the PENDING HUMAN banner, the badge or the DEGRADED note that would hide a safety
// sentence, so the app's own code never sets them (ADR 0008 Amendment 1 (a)). React Aria passes
// an `inert` prop through to the DOM, so a HeroUI component is no exception. A form field's
// `type="hidden"` is a value, not one of these attributes, and is allowed.
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const ATTR = /aria-?hidden|inert|hidden/i.source;

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

function source(dir: string): string[] {
  return walk(join(ROOT, dir)).filter((f) => /\.(tsx?|jsx?|mjs)$/.test(f));
}

// Comments are not code. A line comment here starts a line or follows a space, so a `//` inside
// a string or a template (`${url.protocol}//`) is kept and still read.
const code = (text: string) =>
  text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|\s)\/\/.*$/gm, "$1");

test("no element in app/ or components/ is given aria-hidden, inert or hidden", () => {
  const files = [...source("app"), ...source("components")];
  assert.ok(files.length > 15, `found only ${files.length} files`);
  for (const file of files) {
    const text = code(readFileSync(file, "utf8"));
    // A JSX attribute, with or without a value: `aria-hidden="true"`, `inert`, `hidden={x}`.
    if (file.endsWith("x")) {
      const jsx = new RegExp(`\\s(?:${ATTR})(?=\\s*=|\\s*/?>|\\s+[\\w-]+[\\s=/>])`, "i");
      assert.doesNotMatch(text, jsx, `${file}: a hiding attribute in JSX`);
      assert.doesNotMatch(text, /\{\s*\.\.\./, `${file}: a spread, which could carry one`);
    }
    // A props key, for createElement: `"aria-hidden": true`, `inert: true`, `hidden: x`, or a
    // computed key holding one of the names.
    const key = new RegExp(`(?:^|[{,\\s])["'\`]?(?:${ATTR})["'\`]?\\s*:(?!:)`, "im");
    assert.doesNotMatch(text, key, `${file}: a hiding attribute as a props key`);
    const computed = new RegExp(`\\[\\s*["'\`](?:${ATTR})`, "i");
    assert.doesNotMatch(text, computed, `${file}: a computed key`);
    // A file that builds elements (`createElement`, often imported as `h`) spreads only into an
    // array, never into props, where a spread could carry one of the names.
    if (/\bcreateElement\b/.test(text)) {
      assert.doesNotMatch(text, /(?<!\[)\.\.\./, `${file}: a spread outside an array literal`);
    }
  }
});

test("no code in app/, components/ or lib/ sets a hiding attribute on the page", () => {
  const files = [...source("app"), ...source("components"), ...source("lib")];
  for (const file of files) {
    const text = code(readFileSync(file, "utf8"));
    const dom = /setAttribute|toggleAttribute|innerHTML|outerHTML|insertAdjacentHTML/;
    assert.doesNotMatch(text, dom, `${file}: writes attributes or HTML directly`);
    assert.doesNotMatch(text, /dangerouslySetInnerHTML/, `${file}: raw HTML`);
    const prop = new RegExp(`\\.(?:${ATTR})\\s*(?:=(?!=)|\\|\\|=|\\?\\?=|&&=)`, "i");
    assert.doesNotMatch(text, prop, `${file}: assigns a hiding property`);
  }
});

test("lib/ renders nothing, so its object keys are not element props", () => {
  for (const file of source("lib")) {
    const text = code(readFileSync(file, "utf8"));
    const react = /from\s+["']react(?:-dom)?(?:\/[\w-]+)?["']/;
    assert.doesNotMatch(text, react, `${file} imports React`);
    assert.doesNotMatch(text, /\bcreateElement\b|\bjsxs?\(/, `${file} builds elements`);
    assert.ok(!file.endsWith("x"), `${file} is a JSX file`);
  }
});
