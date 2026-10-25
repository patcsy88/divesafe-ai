import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, test } from "node:test";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import WithheldBox, { WITHHELD_INTRO, WITHHELD_TITLE } from "../components/WithheldBox.ts";
import { ApiError, getEvidence } from "../lib/api.ts";
import { MAX_SHOWN, MAX_TEXT, MAX_WITHHELD_SHOWN, capWithheld, clip, shown } from "../lib/present.ts";
import type { EvidenceItem } from "../lib/types.ts";

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
const page = read("../app/assessments/[id]/evidence/page.tsx");
const box = read("../components/WithheldBox.ts");

const mk = (id: string, over: Partial<EvidenceItem> = {}): EvidenceItem => ({
  id,
  category: "wave",
  source: "src",
  source_version: "1",
  retrieved_at: "2026-10-10T00:00:00Z",
  valid_at: "2026-10-10T00:00:00Z",
  valid_until: null,
  is_forecast: false,
  data_kind: "observation",
  upstream: [],
  upstream_known: true,
  quality: "unassessed",
  quality_notes: [],
  value: {},
  ...over,
});
const render = (items: EvidenceItem[] | undefined, note = "server note") =>
  renderToStaticMarkup(createElement(WithheldBox, { items, note }));

test("nothing renders when the list is empty or absent", () => {
  assert.equal(render([]), "");
  assert.equal(render(undefined), "");
});

test("the box is labelled, separate, and has the fixed intro and the server note", () => {
  const html = render([mk("x")], "the <b>note</b>");
  assert.ok(html.includes('aria-labelledby="withheld-heading"'));
  assert.ok(html.includes(`<h2 id="withheld-heading">${WITHHELD_TITLE}</h2>`));
  assert.ok(html.includes(WITHHELD_INTRO));
  assert.ok(html.includes("the &lt;b&gt;note&lt;/b&gt;"));
  assert.ok(html.indexOf(WITHHELD_INTRO) < html.indexOf("the &lt;b&gt;note"));
});

test("hostile text is escaped, bidi is stripped, long values are cut, and there is no link", () => {
  const evil = "<script>alert(1)</script>\u202E";
  const html = render([
    mk("<img src=x>\u202E", {
      category: evil,
      source: evil + "s".repeat(5000),
      source_version: evil,
      quality_notes: [evil + "n".repeat(5000)],
      value: { v: evil + "z".repeat(MAX_SHOWN * 2) },
    }),
  ]);
  assert.ok(!html.includes("<script"));
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("&lt;script&gt;alert(1)&lt;/script&gt;"));
  assert.ok(!html.includes("\u202E"));
  assert.ok(html.includes("… truncated"));
  assert.ok(!html.includes("s".repeat(MAX_TEXT + 1)));
  assert.ok(!html.includes("n".repeat(MAX_TEXT + 1)));
  assert.ok(!html.includes("z".repeat(MAX_SHOWN + 1)));
  assert.ok(!/<a[\s>]|href=/.test(html));
});

test("items are ordered by id, shown as text, and used for no DOM id or link", () => {
  const html = render([mk("b"), mk("a"), mk("b", { source: "other" })]);
  assert.deepEqual([...html.matchAll(/<dd>([ab])<\/dd>/g)].map((m) => m[1]), ["a", "b", "b"]);
  assert.equal(html.match(/ id="/g)?.length, 1); // only the heading id
  assert.ok(/clip\(it\.id\)/.test(box));
  assert.ok(!/it\.id/.test(box.replace("clip(it.id)", "")));
  assert.ok(!/<Link|<a\s|href|dangerouslySetInnerHTML|innerHTML|"a"/.test(box));
});

test("at most 50 items are shown and the rest are counted exactly", () => {
  const many = Array.from({ length: 53 }, (_, n) => mk(`i${String(n).padStart(3, "0")}`));
  const html = render(many);
  assert.equal(html.match(/aria-label="Withheld item"/g)?.length, MAX_WITHHELD_SHOWN);
  assert.ok(html.includes("<p>3 more withheld items not shown</p>"));
  assert.ok(!render(many.slice(0, 50)).includes("more withheld items"));
  assert.equal(capWithheld(many).hidden, 3);
});

test("clip cuts a text at 1000 characters after stripping control characters", () => {
  assert.equal(clip("a\u202Eb"), "ab");
  assert.equal(clip("x".repeat(1000)).length, 1000);
  assert.ok(clip("x".repeat(1001)).startsWith("x".repeat(1000)));
  assert.ok(clip("x".repeat(1001)).length < 1010);
});

test("the withheld box sits above the normal evidence list, and the page has no withheld logic", () => {
  assert.ok(page.indexOf("<WithheldBox") !== -1);
  assert.ok(page.indexOf("<WithheldBox") < page.indexOf("view.evidence.map("));
  assert.ok(/withheld_evidence\?: EvidenceItem\[\]/.test(read("../lib/types.ts")));
});

const real = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = real;
});
const ID = "a".repeat(32);
const reply = (body: unknown) => {
  globalThis.fetch = (async () => new Response(JSON.stringify(body), { status: 200 })) as typeof fetch;
};

test("an evidence reply about another assessment, or without an id, is refused", async () => {
  for (const body of [{ assessment_id: "b".repeat(32) }, {}, [], null, "x"]) {
    reply(body);
    await assert.rejects(
      () => getEvidence("k-12345678", ID),
      (e: unknown) => e instanceof ApiError && /does not recognise/.test(e.message),
    );
  }
  reply({ assessment_id: ID, evidence: [] });
  assert.equal((await getEvidence("k-12345678", ID)).assessment_id, ID);
});
