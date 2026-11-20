import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import DegradedEvidenceNote, {
  DEGRADED_EVIDENCE_NOTE,
  degradedContext,
} from "../components/DegradedEvidenceNote.ts";

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
const render = (note: unknown) => renderToStaticMarkup(createElement(DegradedEvidenceNote, { note }));

// Pinned word for word here and, against the API constant, in
// tests/safety/test_degraded_support_policy.py.
const NOTE = "based on model and forecast data, not observations";

test("the fixed wording is pinned word for word", () => {
  assert.equal(DEGRADED_EVIDENCE_NOTE, NOTE);
});

test("the note renders only for a non-empty string, and always as the app's own wording", () => {
  for (const note of [undefined, null, "", false, 0, {}, ["x"]]) {
    assert.equal(render(note), "", `note=${String(note)}`);
    assert.equal(degradedContext(note), "");
  }
  for (const note of [NOTE, "<b>other text</b>"]) {
    const html = render(note);
    assert.ok(html.includes(`This result is ${NOTE}.`));
    assert.ok(html.includes('data-testid="degraded-evidence-note"'));
    assert.ok(!html.includes("other text"));
    assert.equal(degradedContext(note), ` This result is ${NOTE}.`);
  }
});

test("the assessment page shows the note by the recommendation and in the decision context", () => {
  const page = read("../app/assessments/[id]/page.tsx");
  const badge = page.indexOf("{labelFor(r.final_recommendation)}</p>");
  const note = page.indexOf("<DegradedEvidenceNote note={view.degraded_evidence_note} />");
  assert.ok(badge > 0 && note > badge && note - badge < 120);
  assert.ok(page.includes("${degradedContext(view.degraded_evidence_note)}"));
});

test("the API types carry the note as a string", () => {
  assert.ok(read("../lib/types.ts").includes("degraded_evidence_note?: string;"));
});
