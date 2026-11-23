import { test } from "node:test";
import assert from "node:assert/strict";
import { lstatSync, readdirSync, readFileSync } from "node:fs";
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
  assert.ok(!lstatSync(join(ROOT, dir)).isSymbolicLink(), `${dir}/ is a symbolic link`);
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
// opened. A new <details> fails; what an existing one holds is not checked (ADR 0017 Known gaps).
const DETAILS = [
  ["app/assessments/[id]/evidence/page.tsx", 1],
  ["components/WithheldBox.ts", 1],
];

// What an existing <details> must never hold: the result, the banners or the DEGRADED note.
const NOT_IN_DETAILS = /Banner|Degraded|DEGRADED|badgeClass|ds-pending|ds-badge|Notice|role/i;

// The whole text of each existing <details>, whitespace folded, by file (owner, 2026-11-23). A
// variable, a child component, an attribute or a comment added inside one changes the text and
// fails here, so nothing reaches a closed <details> unreviewed.
const DETAILS_BODIES = [
  [
    "app/assessments/[id]/evidence/page.tsx",
    '<details> <summary>Raw values (third-party text, shown as plain text)</summary> <pre ' +
      'className="ds-value">{shown(item.value)}</pre> ',
  ],
  [
    "components/WithheldBox.ts",
    'h( "details", null, h("summary", null, "Raw values (third-party text, shown as plain ' +
      'text)"), h("pre", { className: "ds-value" }, shown(it.value)), ',
  ],
];

// Every write through a computed name (`obj[key] = v`) in the three folders, by file. The name is
// not visible here, so each is reviewed: this one fills a plain object of form numbers.
const COMPUTED_WRITES = [["lib/observations.ts", "observations[key] ="]];

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

// Components this file may use as tags: its imports and its own function declarations.
function components(text: string): Set<string> {
  const names = [...text.matchAll(/^import\s+(?:type\s+)?(\w+)/gm)].map((m) => m[1] ?? "");
  for (const m of text.matchAll(/^import\s+(?:type\s+)?(?:\w+\s*,\s*)?\{([^}]*)\}/gm)) {
    for (const part of (m[1] ?? "").split(",")) names.push(part.trim().split(/\s+/).pop() ?? "");
  }
  const own = /^(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Z]\w*)/gm;
  for (const m of text.matchAll(own)) names.push(m[1] ?? "");
  return new Set(names.filter((n) => n !== ""));
}

// The text of each <details>: in JSX up to its closing tag; for `h("details", ...)` up to the
// bracket that closes the call (counting round brackets outside strings). One that cannot be
// read whole fails.
function detailsBlocks(file: string, text: string): string[] {
  const blocks: string[] = [];
  for (const m of text.matchAll(/<details\b/gi)) {
    const end = text.indexOf("</details>", m.index);
    assert.ok(end > 0, `${file}: a <details> with no closing tag`);
    blocks.push(text.slice(m.index, end));
  }
  for (const m of text.matchAll(/\b(?:h|createElement)\(\s*["'`]details\b/gi)) {
    let depth = 0;
    let quote = "";
    let end = -1;
    for (let i = m.index + m[0].indexOf("("); i < text.length && end < 0; i++) {
      const c = text[i];
      if (quote) {
        if (c === "\\") i++;
        else if (c === quote) quote = "";
      } else if (c === '"' || c === "'" || c === "`") quote = c;
      else if (c === "(") depth++;
      else if (c === ")" && --depth === 0) end = i;
    }
    assert.ok(end > 0, `${file}: an h("details") call that cannot be read whole`);
    blocks.push(text.slice(m.index, end));
  }
  return blocks;
}

// A failure on text that is not an attribute is fixed by rewording the text, never by loosening
// a pattern here (ADR 0017).
const REWORD = "if this is text, not an attribute, reword it; do not loosen the pattern";

// Every assignment operator, plain or compound (`=`, `+=`, `|=`, `**=`, `||=`, ...).
const ASSIGN = /(?:\*\*|<<|>>>?|&&|\|\||\?\?|[-+*\/%&|^])?=(?![=>])/.source;

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
  const bodies: string[][] = [];
  for (const file of files) {
    const text = read(file);
    const name = file.slice(ROOT.length + 1);
    // A JSX attribute, with or without a value: `aria-hidden="true"`, `inert`, `hidden={x}`.
    if (file.endsWith("x")) {
      const jsx = new RegExp(`\\s(?:${ATTR})(?=\\s*=|\\s*/?>|\\s+[\\w-]+[\\s=/>])`, "i");
      assert.doesNotMatch(text, jsx, `${file}: a hiding attribute in JSX (${REWORD})`);
      assert.doesNotMatch(text, /\{\s*\.\.\./, `${file}: a spread, which could carry one`);
    }
    // HTML attribute and tag names ignore case, so the checks below do too.
    // Elements the browser keeps closed or never shows, and inline styles, which could hide text.
    const shut = /(?:dialog|template|noscript|datalist|svg)\b/.source;
    const closed = new RegExp(`<${shut}|\\b(?:h|createElement)\\(\\s*["'\`]${shut}`, "i");
    assert.doesNotMatch(text, closed, `${file}: a dialog or template`);
    const style = /\bstyle["'`]?\s*[=:]|\.style\b|cssText/i;
    assert.doesNotMatch(text, style, `${file}: an inline style`);
    // A role of none or presentation removes what a screen reader announces for an element.
    const role = /\brole["'`]?\s*[=:]\s*\{?\s*["'`](?:none|presentation)/i;
    assert.doesNotMatch(text, role, `${file}: a role that removes the element's meaning`);
    const opened = /<details\b|\b(?:h|createElement)\(\s*["'`]details\b/gi;
    const n = text.match(opened)?.length ?? 0;
    if (n > 0) details.push([name, n]);
    for (const block of detailsBlocks(file, text)) {
      assert.doesNotMatch(block, NOT_IN_DETAILS, `${file}: a safety sentence inside a <details>`);
      bodies.push([name, block.replace(/\s+/g, " ")]);
    }
    // A capitalised tag is a component: one imported or declared in this file, never a name
    // holding a tag such as `const T = "dialog"`. A type argument (`useRef<T>`) is not a tag.
    const known = components(text);
    const tags = /(?<![\w$.])<([A-Z][\w$]*)|(?<![\w.])(?:h|createElement)\(\s*([A-Z][\w$]*)/g;
    for (const m of text.matchAll(tags)) {
      const t = m[1] ?? m[2] ?? "";
      assert.ok(known.has(t), `${file}: <${t}> is not an imported or declared component`);
    }
    const label = /(aria-label(?:ledby)?)(?:["'`]\s*:|\s*=)\s*(?:"([^"]*)"|'([^']*)'|(.))/gi;
    for (const m of text.matchAll(label)) {
      assert.equal(m[4], undefined, `${file}: an ${m[1]} that is not a literal`);
      labels.push([name, m[1] ?? "", m[2] ?? m[3] ?? ""]);
    }
    assert.equal(
      text.match(/aria-label/gi)?.length ?? 0,
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
  const held = "what a <details> holds changed: each change needs review (ADR 0017)";
  assert.deepEqual(bodies.sort(), DETAILS_BODIES, held);
});

test("no code in app/, components/ or lib/ sets a hiding attribute on the page", () => {
  const files = [...source("app"), ...source("components"), ...source("lib")];
  const writes: string[][] = [];
  for (const file of files) {
    const text = read(file);
    const name = file.slice(ROOT.length + 1);
    // A name held in a variable (`const k = "inert"; el[k] = true`) is still written out as a
    // string, so the names are refused as strings too. A form field's `type="hidden"` is allowed.
    // An escape could spell one (`in\u0065rt`, `in\x65rt`): `\u` and a `\x` letter are refused.
    assert.doesNotMatch(text, /\\u/, `${file}: a \\u escape, which these checks cannot read`);
    const letter = /\\x(?:4[1-9a-f]|5[0-9a]|6[1-9a-f]|7[0-9a])/i;
    assert.doesNotMatch(text, letter, `${file}: a \\x letter escape, which these cannot read`);
    const field = text.replace(/\btype\s*[=:]\s*["']hidden["']/g, "");
    const quoted = new RegExp(`(["'\`])(?:${ATTR})\\1`, "i");
    assert.doesNotMatch(field, quoted, `${file}: a hiding name as a string (${REWORD})`);
    // A tag name kept as a string (`export const Shut = "dialog"`) could be used as a tag
    // elsewhere, so the closed elements' names never appear as strings, and "details" only in h().
    const shut = /(["'`])(?:dialog|template|noscript|datalist|svg)\1/i;
    assert.doesNotMatch(text, shut, `${file}: a closed element's name as a string (${REWORD})`);
    const asString = text.match(/(["'`])details\1/gi)?.length ?? 0;
    const inH = text.match(/\b(?:h|createElement)\(\s*["'`]details["'`]/gi)?.length ?? 0;
    assert.equal(asString, inH, `${file}: "details" as a string outside h() (${REWORD})`);
    // Code built from a string is never read by these checks.
    const built = /\beval\b|\bFunction\s*\(|\bset(?:Timeout|Interval)\s*\(\s*["'`]/;
    assert.doesNotMatch(text, built, `${file}: runs code built from a string`);
    // Every write through a computed member (`el[k] = v`, `el [k] |= 1`, `el[k]++`, `++el[k]`,
    // `[el[k]] = x`), whose name this cannot see. A declaration (`const [a, b] =`) is not one.
    const word = /(?!(?:const|let|var|return|of|in|await|yield)\s)/.source;
    const target = new RegExp(`(?<![\\w$])${word}[\\w$)\\]]+\\s*\\[[^\\]\\n]+\\]`);
    const member = new RegExp(
      [
        `${target.source}\\s*(?:${ASSIGN}|\\+\\+|--)`,
        `(?:\\+\\+|--)\\s*${target.source}`,
        `[\\[{,]\\s*${target.source}\\s*[,\\]}]`,
      ].join("|"),
      "g",
    );
    for (const m of text.matchAll(member)) writes.push([name, m[0].replace(/\s+/g, " ")]);
    const dom = /setAttribute|toggleAttribute|innerHTML|outerHTML|insertAdjacentHTML/;
    assert.doesNotMatch(text, dom, `${file}: writes attributes or HTML directly`);
    assert.doesNotMatch(text, /dangerouslySetInnerHTML/, `${file}: raw HTML`);
    // Any write to a hiding property: plain, compound (`|=`, `+=`), `++`/`--` on either side, or
    // as a destructuring target (`[el.hidden] = [x]`, `({ a: el.inert } = y)`). Reading one, as
    // `document.hidden` is read, is allowed outside an array or object literal.
    const names = `(?:${ATTR}|ariaLabel(?:ledBy)?)`;
    const prop = new RegExp(
      [
        `\\.${names}\\s*(?:${ASSIGN}|\\+\\+|--)`,
        `(?:\\+\\+|--)\\s*[\\w$.]*\\.${names}\\b`,
        `\\.${names}\\s*[,\\]}]`,
      ].join("|"),
      "i",
    );
    assert.doesNotMatch(text, prop, `${file}: assigns a hiding property`);
    const bracket = new RegExp(`\\[\\s*["'\`]${names}["'\`]\\s*\\]\\s*(?:${ASSIGN})`, "i");
    assert.doesNotMatch(text, bracket, `${file}: assigns a hiding property by name`);
    const indirect = /Object\.assign|Reflect\.set|defineProperty|defineProperties/;
    assert.doesNotMatch(text, indirect, `${file}: sets properties indirectly`);
    // Classes: classes.test.ts allows only ds- classes styled in globals.css, in className
    // literals; the DOM routes around it are refused here.
    assert.doesNotMatch(text, /classList|\.className\s*=/, `${file}: sets classes on the page`);
    // Removing text from the page, or closing a <details>.
    const remove = new RegExp(
      [
        /(?:textContent|innerText|outerText|nodeValue)\s*=(?!=)/.source,
        /replaceChildren|replaceWith|removeChild|\.remove\(\)|\.(?:open|data)\s*=(?!=)/.source,
      ].join("|"),
    );
    assert.doesNotMatch(text, remove, `${file}: removes text from the page`);
    // Removing an attribute closes a <details> (`open`) or drops an alert's `role`.
    const unset = /removeAttribute|\.attributes\b/;
    assert.doesNotMatch(text, unset, `${file}: removes attributes from the page`);
    // tests/ is not read by these checks, so app code never imports from it.
    const fromTests = /(?:from\s+|import\s*\(\s*|require\s*\(\s*)["'][^"']*\/tests\//;
    assert.doesNotMatch(text, fromTests, `${file}: imports from tests/`);
  }
  const changed = "a write through a computed name changed: each needs review (ADR 0017)";
  assert.deepEqual(writes.sort(), COMPUTED_WRITES, changed);
});

// lib/ renders nothing and does not look up the page (it could still change an element it is
// given, which the test above covers); and since every props argument is a literal in the file
// that renders it, a lib/ object never becomes props.
test("lib/ renders nothing", () => {
  assert.ok(source("lib").length > 5, "found almost no files in lib/");
  for (const file of source("lib")) {
    const text = read(file);
    const react = /(?:from\s+|(?:require|import)\s*\(\s*)["']react(?:-dom)?(?:\/[\w-]+)?["']/;
    assert.doesNotMatch(text, react, `${file} imports React`);
    assert.doesNotMatch(text, /\bcreateElement\b|\bjsxs?\(/, `${file} builds elements`);
    const page = /\b(?:document|window)\b|querySelector|getElementBy/;
    assert.doesNotMatch(text, page, `${file} looks up the page`);
    assert.ok(!file.endsWith("x"), `${file} is a JSX file`);
  }
});
