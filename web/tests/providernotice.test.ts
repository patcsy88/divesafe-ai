import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ProviderNotices, {
  PROVIDER_NOTICE_MISSING_TEXT,
  PROVIDER_NOTICE_NOTE,
  PROVIDER_NOTICE_TITLE,
} from "../components/ProviderNotices.ts";

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
const render = (notices: unknown, notShown?: unknown, missing?: unknown) =>
  renderToStaticMarkup(
    createElement(ProviderNotices, {
      notices: notices as string[] | undefined,
      notShown: notShown as number | undefined,
      missing: missing as boolean | undefined,
    }),
  );

// Pinned word for word here and, against the API constant, in
// tests/safety/test_tides_today_full_notice.py.
const NOTE =
  "Text supplied by the data provider, shown as received. It is the provider's own copyright and licence notice, shown because this assessment retrieved tides.today data (that data may have been withheld; see Evidence, if your key can read it). DiveSafe has not checked it. It does not validate the data, does not describe the tide at the dive site, and does not change the result.";
const MISSING = "provider notice missing for a tides.today item";
const MISSING_P = `<p data-testid="provider-notice-missing">${MISSING}</p>`;
const NOTE_HTML = NOTE.replace("provider's", "provider&#x27;s");
const SECTION_OPEN = 'data-testid="provider-notices"';

// The provider block: from the opening <section> to its closing tag ("" when there is none).
const block = (html: string): string => {
  const start = html.indexOf("<section");
  if (start < 0) return "";
  const end = html.indexOf("</section>", start);
  assert.ok(end > start, "the provider block is not closed");
  return html.slice(start, end + "</section>".length);
};
const count = (html: string, part: string): number => html.split(part).length - 1;

test("nothing renders without a notice or the flag", () => {
  for (const notices of [[], undefined, "not an array", null, { length: 3 }]) {
    assert.equal(render(notices), "");
    assert.equal(render(notices, 3), "");
    assert.equal(render(notices, Infinity), "");
    assert.equal(render(notices, 3, false), "");
  }
});

test("the flag counts only when it is exactly true", () => {
  for (const missing of [undefined, false, "true", 1, {}, null]) {
    assert.equal(render([], 0, missing), "", `missing=${String(missing)}`);
    assert.ok(!render(["x"], 0, missing).includes(MISSING), `missing=${String(missing)}`);
  }
});

test("the fixed wording is pinned word for word and the note is shown under the heading", () => {
  assert.equal(PROVIDER_NOTICE_NOTE, NOTE);
  assert.equal(PROVIDER_NOTICE_MISSING_TEXT, MISSING);
  assert.equal(PROVIDER_NOTICE_TITLE, "Provider notice (tides.today)");
  const html = render(["x"]);
  const inside = block(html);
  assert.ok(inside.includes(`<p class="ds-hint" role="note">${NOTE_HTML}</p>`));
  assert.ok(inside.indexOf(`>${PROVIDER_NOTICE_TITLE}</h2>`) < inside.indexOf("Text supplied by"));
  assert.ok(inside.indexOf("Text supplied by") < inside.indexOf(">x</pre>"));
  assert.equal(count(html, "Text supplied by"), 1);
});

test("the flag alone renders only the missing sentence: no block, heading, caption or text", () => {
  for (const notices of [[], undefined, null, "not an array"]) {
    const html = render(notices, 0, true);
    assert.equal(html, MISSING_P, `notices=${String(notices)}`);
    assert.ok(!html.includes(SECTION_OPEN), "a provider block was rendered for the flag alone");
    assert.ok(!html.includes("<section"));
    assert.ok(!html.includes(PROVIDER_NOTICE_TITLE), "the provider heading was rendered");
    assert.ok(!html.includes("Text supplied by"), "the provider caption was rendered");
    assert.ok(!html.includes("<pre"), "the missing sentence was shown as provider text");
  }
  // A count with no provider text says nothing about notices not shown.
  assert.equal(render([], 5, true), MISSING_P);
  assert.equal(render([], Infinity, true), MISSING_P);
});

test("with notices and the flag, the sentence is shown once, after and outside the block", () => {
  const html = render(["PROVIDER TEXT"], 2, true);
  const inside = block(html);
  assert.ok(inside.includes(SECTION_OPEN));
  assert.ok(inside.includes('<pre class="ds-provider-notice-text">PROVIDER TEXT</pre>'));
  assert.ok(inside.includes(NOTE_HTML));
  assert.ok(inside.includes(">2 more provider notices not shown</p>"));
  assert.ok(!inside.includes(MISSING), "the missing sentence is inside the provider block");
  assert.equal(html, inside + MISSING_P, "anything but the block and the sentence was rendered");
  assert.equal(count(html, MISSING), 1, "the missing sentence is not shown exactly once");
});

test("without the flag no missing sentence is rendered", () => {
  const html = render(["PROVIDER TEXT"], 0, false);
  assert.ok(!html.includes("provider-notice-missing"));
  assert.ok(!html.includes(MISSING));
  assert.equal(html, block(html), "something besides the provider block was rendered");
});

test("provider text equal to the missing sentence is provider text, inside the block", () => {
  const html = render([MISSING]);
  assert.ok(block(html).includes(`<pre class="ds-provider-notice-text">${MISSING}</pre>`));
  assert.ok(!html.includes("provider-notice-missing"));
  assert.equal(count(html, MISSING), 1);
});

test("the notice text uses the CSS class, no inline style, and is not a scroll box", () => {
  const html = render(["x".repeat(2000)], 1, true);
  assert.ok(html.includes('<section class="ds-card"'));
  assert.ok(html.includes(`<pre class="ds-provider-notice-text">${"x".repeat(2000)}</pre>`));
  assert.ok(!html.includes("style="), "an inline style is set");
  assert.ok(!html.includes("overflow") && !html.includes("max-height"));
  assert.ok(!html.includes('class="ds-value"'));
  const css = read("../app/globals.css");
  const rules = css.split("\n").filter((line) => line.includes("provider-notice-text"));
  assert.equal(rules.length, 1, "the class is not styled exactly once");
  const rule = rules[0] ?? "";
  assert.ok(rule.startsWith("pre.ds-provider-notice-text {"), rule);
  assert.ok(rule.includes("white-space:pre-wrap"), rule);
  assert.ok(rule.includes("overflow-wrap:anywhere"), rule);
  assert.ok(!rule.includes("max-height") && !/overflow\s*:/.test(rule), rule);
});

test("a count of notices not shown is said in fixed words, only when finite and positive", () => {
  assert.ok(render(["x"], 3).includes("<p>3 more provider notices not shown</p>"));
  assert.ok(render(["x"], 2.7).includes("<p>2 more provider notices not shown</p>"));
  assert.ok(block(render(["x"], 3, true)).includes("3 more provider notices not shown"));
  for (const bad of [0, -2, 0.5, Number.NaN, Infinity, -Infinity, "3", undefined, null, {}]) {
    const html = render(["x"], bad);
    assert.ok(!html.includes("more provider notices"), `count ${String(bad)}`);
    assert.ok(!html.includes("Infinity") && !html.includes("NaN"), `count ${String(bad)}`);
    assert.ok(html.includes('<pre class="ds-provider-notice-text">x</pre>'), `count ${String(bad)}`);
  }
});

test("the full text is shown uncut, one block per notice, in the order given", () => {
  const long = "n".repeat(1900);
  const html = render([long, "second"]);
  assert.ok(html.includes(`>${PROVIDER_NOTICE_TITLE}</h2>`));
  assert.ok(html.includes(`<pre class="ds-provider-notice-text">${long}</pre>`));
  assert.equal(count(html, "<pre "), 2);
  assert.ok(html.indexOf(long) < html.indexOf(">second</pre>"));
  assert.equal(count(html, "<section"), 1);
});

test("markup is shown as text, control and bidi characters are removed, no link is made", () => {
  const html = render(["<script>alert(1)</script> <a href=\"x\">y</a>\u202e\u0007 end"], 0, true);
  assert.ok(html.includes("&lt;script&gt;alert(1)&lt;/script&gt;"));
  assert.ok(!html.includes("<script"));
  assert.ok(!html.includes("<a "));
  assert.ok(!html.includes("\u202e") && !html.includes("\u0007"));
});

test("the assessment page renders the notices, the count and the flag, as text", () => {
  const page = read("../app/assessments/[id]/page.tsx");
  assert.ok(page.includes("notices={view.provider_notices}"));
  assert.ok(page.includes("notShown={view.provider_notices_not_shown}"));
  assert.ok(page.includes("missing={view.provider_notice_missing}"));
  assert.ok(!page.includes("dangerouslySetInnerHTML"));
});

test("the API types carry the flag as a boolean, never as notice text", () => {
  const types = read("../lib/types.ts");
  assert.ok(types.includes("provider_notice_missing?: boolean;"));
  assert.ok(types.includes("provider_notices?: string[];"));
  assert.ok(types.includes("provider_notices_not_shown?: number;"));
});
