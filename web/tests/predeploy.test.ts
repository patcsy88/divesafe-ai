import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { describeFailure } from "../lib/api.ts";
import {
  DEGRADED_NOTE_MISSING,
  degradedNoteMissing,
  KNOWLEDGE_NOTE_MISSING,
  knowledgeText,
  NO_VISIBLE_REASON,
  reasonShown,
  withheldCount,
} from "../lib/notes.ts";
import { normaliseNewlines, parseObservations, UNSAFE_NAME, UNSAFE_TEXT } from "../lib/observations.ts";
import { formatWhen, isLessSevere, MOST_SEVERE_FIRST } from "../lib/present.ts";

// Pre-deploy review fixes (owner #69a, 2026-11-23).
const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
const page = read("../app/assessments/[id]/page.tsx");
const actions = read("../app/actions.ts");
const body = (name: string) => {
  const start = actions.indexOf(`export async function ${name}`);
  assert.ok(start >= 0, name);
  const next = actions.indexOf("\nexport ", start + 1);
  return actions.slice(start, next < 0 ? undefined : next);
};

test("the site-document note is shown, and a missing one is never read as 'nothing to say'", () => {
  assert.equal(knowledgeText(""), null);
  assert.equal(knowledgeText("No site documents are configured."), "No site documents are configured.");
  assert.equal(knowledgeText("a\u202Eb"), "ab");
  for (const missing of [undefined, null, 0, false, {}, ["x"]]) {
    assert.equal(knowledgeText(missing), KNOWLEDGE_NOTE_MISSING, String(missing));
  }
  assert.ok(page.includes("const knowledge = knowledgeText(view.knowledge_note);"));
  assert.ok(page.includes("const knowledgeMissing = knowledge === KNOWLEDGE_NOTE_MISSING;"));
  assert.ok(page.includes('{knowledge && !knowledgeMissing && <p role="note">{plain(knowledge)}</p>}'));
  assert.ok(page.includes('<p className="ds-pending" role="note">\n            {plain(KNOWLEDGE_NOTE_MISSING)}'));
  assert.ok(page.includes("${knowledgeMissing ? ` ${KNOWLEDGE_NOTE_MISSING}` : \"\"}`}"), "in the form context");
  assert.ok(read("../lib/types.ts").includes("knowledge_note?: string;"));
});

test("a GO or CAUTION without the degraded-evidence field is treated as resting on model data", () => {
  for (const rec of ["GO", "CAUTION"]) {
    for (const missing of [undefined, null, 0, false]) assert.ok(degradedNoteMissing(rec, missing), rec);
    assert.equal(degradedNoteMissing(rec, ""), false);
    assert.equal(degradedNoteMissing(rec, "based on model and forecast data, not observations"), false);
  }
  for (const rec of ["NO-GO", "INSUFFICIENT EVIDENCE"]) assert.equal(degradedNoteMissing(rec, undefined), false);
  assert.match(DEGRADED_NOTE_MISSING, /Treat it as if it does/);
  const badge = page.indexOf("{labelFor(r.final_recommendation)}</p>");
  const missing = page.indexOf("degradedNoteMissing(r.final_recommendation, view.degraded_evidence_note)");
  assert.ok(badge > 0 && missing > badge && missing - badge < 260, "the note sits by the badge");
  assert.ok(
    page.includes("${degradedNoteMissing(r.final_recommendation, view.degraded_evidence_note) ? ` ${DEGRADED_NOTE_MISSING}` : \"\"}$"),
    "the decision form's context carries it too",
  );
});

test("pending, withheld and decided states come before the system's badge", () => {
  const badge = page.indexOf("<p className={badgeClass(r.final_recommendation)}>");
  for (const marker of ["PENDING HUMAN DECISION.", "DECISION NOT SHOWN TO THIS KEY.", "HUMAN DECISION RECORDED:"]) {
    const at = page.indexOf(marker);
    assert.ok(at > 0 && at < badge, `${marker} must come before the badge`);
  }
  assert.ok(page.includes('"System recommendation (not yet decided by a human)"'));
});

test("the decision form comes after every problem section and the model commentary", () => {
  const form = page.indexOf("<DecisionForm");
  assert.ok(form > 0);
  for (const heading of [
    "<h2>Sources that failed or were rejected</h2>",
    "<h2>Conflicting evidence set aside</h2>",
    "<h2>Not evaluated</h2>",
    "<h2>Rule results</h2>",
    "<h2>Model-written commentary</h2>",
    "<h2>Questions and conflicting signals (model-written)</h2>",
    "<h2>Model calls that failed, and model-server notes</h2>",
  ]) {
    const at = page.indexOf(heading);
    assert.ok(at > 0 && at < form, `${heading} must come before the form`);
  }
  assert.equal(page.split("<DecisionForm").length, 2, "one form");
});

test("the decision choices run from most to least severe", () => {
  assert.deepEqual([...MOST_SEVERE_FIRST], ["NO-GO", "INSUFFICIENT EVIDENCE", "CAUTION", "GO"]);
  const form = read("../components/DecisionForm.tsx");
  assert.ok(form.includes("{MOST_SEVERE_FIRST.map((r) => ("));
  assert.ok(!/const MOST_SEVERE_FIRST/.test(form), "the form keeps no order of its own");
  assert.ok(isLessSevere("GO", "NO-GO") && isLessSevere("INSUFFICIENT EVIDENCE", "NO-GO"));
  assert.ok(!isLessSevere("NO-GO", "INSUFFICIENT EVIDENCE"));
});

test("a reason with invisible, control or text-direction characters is refused before the API", () => {
  for (const bad of [
    "\u200B", "ok\u200B", "a\u202Eb", "a\u0007b", "\u2066x", "a\u00ADb",
    "a\u2028b", "a\u2029b", "\uE000", "\u0378", "\u3164", "\u115F", "\u1160", "\uFFA0", "\u2800",
  ]) {
    assert.ok(UNSAFE_TEXT.test(bad), JSON.stringify(bad));
  }
  for (const good of [
    "Swell dropped since the forecast.", "two\nlines", "tab\there", "Malaysia (UTC+8)",
    "浪很小", "அலை குறைவு", "Ombak 1.5 m — tenang", "café",
  ]) {
    assert.ok(!UNSAFE_TEXT.test(good), JSON.stringify(good));
  }
  const fn = body("submitDecision");
  const check = fn.indexOf("if (UNSAFE_TEXT.test(rationale)) {");
  assert.ok(fn.includes('const rationale = normaliseNewlines(text(data, "rationale"));'));
  assert.ok(check > 0 && check < fn.indexOf("await recordDecision("));
  // A non-empty reason with nothing visible is refused before it is recorded (write-once).
  const blank = fn.indexOf("if (rationale && reasonShown(rationale) === NO_VISIBLE_REASON) {");
  assert.ok(blank > check && blank < fn.indexOf("await recordDecision("));
  const refused = (r: string) => UNSAFE_TEXT.test(r) || reasonShown(r) === NO_VISIBLE_REASON;
  for (const bad of ["\u0301\u0301", " \u0301 ", "\u3164"]) assert.ok(refused(bad), JSON.stringify(bad));
  for (const good of ["浪很小", "அலை குறைவு", ".", "Calm, 1 m swell"]) assert.ok(!refused(good), good);
});

test("browser line breaks (CRLF) are normalised, so a two-line note or reason is accepted", () => {
  assert.equal(normaliseNewlines("a\r\nb\rc\nd"), "a\nb\nc\nd");
  const r = parseObservations((name) => (name === "notes" ? "line one\r\nline two" : ""));
  assert.deepEqual(r, { ok: true, observations: { notes: "line one\nline two" } });
  assert.equal(parseObservations((n) => (n === "notes" ? "a\u202Eb" : "")).ok, false);
});

test("a stored reason with no visible text is shown as no reason, never as an empty row", () => {
  for (const blank of ["\u200B", "\u200B\u200B", "   ", "\u202E", "\u3164", "\u2800 \u115F", "\u0301\u0301"]) {
    assert.equal(reasonShown(blank), NO_VISIBLE_REASON, JSON.stringify(blank));
  }
  for (const seen of ["Calm at the site.", "浪很小", ".", "1"]) assert.equal(reasonShown(seen), seen);
  assert.ok(page.includes("{plain(reasonShown(decision.override_rationale))}"));
});

test("set-aside conflicting items are counted on the assessment page and in the decision context", () => {
  assert.equal(withheldCount(undefined), 0);
  assert.equal(withheldCount("x"), 0);
  assert.equal(withheldCount([{}, {}]), 2);
  assert.ok(page.includes("const setAside = withheldCount(r.withheld_evidence);"));
  assert.ok(page.includes("{setAside > 0 && ("));
  assert.ok(page.includes("${setAside} conflicting item(s) set aside (see Evidence)."));
});

test("a model finding that cites no evidence is marked", () => {
  assert.ok(page.includes('{f.no_evidence ? " (cites no evidence)" : ""}'));
  assert.ok(read("../lib/types.ts").includes("no_evidence?: boolean;"));
});

test("every server action refuses a call that is not a form, before anything else", () => {
  for (const name of ["login", "runAssessment", "submitDecision", "submitActualConditions"]) {
    const fn = body(name);
    const guard = fn.indexOf("if (!(data instanceof FormData)) return INVALID;");
    assert.ok(guard > 0, name);
    assert.ok(guard < fn.indexOf("data", fn.indexOf("{") + 1) + 60, `${name}: guard first`);
    const key = fn.indexOf("requireKey()");
    if (key >= 0) assert.ok(guard < key, `${name}: guard before the session`);
  }
});

test("site and decider are bounded in the app as well as by the API", () => {
  const fn = body("runAssessment");
  assert.ok(fn.includes('if (!/^[A-Za-z0-9._-]{1,100}$/.test(site)) return { error: "Choose a registered site." };'));
  assert.ok(fn.includes("if (decider.length > 100 || UNSAFE_NAME.test(decider)) {"));
  for (const bad of ["a\nb", "a\tb", "a\u202Eb", "\u3164"]) assert.ok(UNSAFE_NAME.test(bad), JSON.stringify(bad));
  for (const good of ["Siti binti Ali", "李明", "O'Neil-Smith"]) assert.ok(!UNSAFE_NAME.test(good), good);
});

test("Retry-After is shown only as a whole number of seconds", () => {
  assert.match(describeFailure(429, null, "12"), /Wait 12 seconds/);
  assert.match(describeFailure(429, null, "86400"), /Wait 86400 seconds/);
  for (const bad of [null, "", "soon<b>", "1.5", "-3", "86401", "999999", "Wed, 21 Oct 2026 07:28:00 GMT"]) {
    assert.equal(describeFailure(429, null, bad), "Too many requests. Wait a moment and retry.", String(bad));
  }
});

test("times are shown in UTC and in Malaysia time", () => {
  assert.equal(formatWhen("2026-11-23T01:30:00Z"), "2026-11-23 01:30 UTC (2026-11-23 09:30 UTC+8)");
  assert.equal(formatWhen("2026-11-23T20:00:00Z"), "2026-11-23 20:00 UTC (2026-11-24 04:00 UTC+8)");
  assert.equal(formatWhen("nonsense"), "unknown time");
});

test("only this app's own pages, or a typed address, can sign the person out", () => {
  const route = read("../app/logout/route.ts");
  assert.ok(route.includes('const OWN = new Set(["same-origin", "none"]);'));
  const check = route.indexOf("if (site !== null && !OWN.has(site)) {");
  assert.ok(check > 0 && check < route.indexOf("await endSession();"));
  // A refused sign-out must not redirect: back into a page with a rejected key it would loop.
  const refused = route.slice(check, route.indexOf("await endSession();"));
  assert.ok(refused.includes("return new Response(REFUSED, {") && refused.includes('"content-type": "text/plain'));
  assert.ok(!/location|redirect|30[1278]/.test(refused), "a refused sign-out does not redirect");
});

test("a page or the root layout that fails to render shows fixed text that says not to act on it", () => {
  for (const file of ["../app/error.tsx", "../app/global-error.tsx"]) {
    const err = read(file);
    assert.ok(err.startsWith('"use client";'), file);
    assert.ok(err.replace(/\s+/g, " ").includes("NO RESULT. This page failed to load. Do not act on it."), file);
    assert.ok(!/\berror\b\s*[.}]|props|digest|message|\(\s*\{/.test(err), `${file}: no error detail`);
  }
  assert.ok(read("../app/global-error.tsx").includes("<html lang=\"en\">"));
});

test("the decided line is worded from the checked outcomes and flags a less severe override", () => {
  assert.ok(page.includes("{plain(decidedNote)}."));
  const note = page.slice(page.indexOf("const decidedNote"), page.indexOf("const decidedNote") + 400);
  assert.ok(note.includes("decision.decision === r.final_recommendation"));
  assert.ok(note.includes("view.overrides_to_less_severe"));
  assert.ok(note.includes('" (an override to a LESS SEVERE outcome than the system recommended)"'));
  assert.ok(!note.includes("is_override"));
});

test("the proposal note is worded from the outcomes, not from llm_tightened", () => {
  const at = page.indexOf("const proposalNote");
  const note = page.slice(at, at + 330);
  assert.ok(note.includes("isLessSevere(r.proposed_recommendation"));
  assert.ok(!note.includes("llm_tightened"));
});

test("a key that cannot open the evidence page is told to ask the decider", () => {
  assert.ok(page.includes('? "Ask the assigned decider, who can read them on the Evidence page."'));
  assert.ok(page.includes("{plain(setAsideAction)}"));
});

test("wording fixes from the review", () => {
  assert.ok(page.includes('"Data attribution (supplied by the provider, not checked): "') ||
    page.includes("Data attribution (supplied by the provider, not checked):"));
  assert.ok(page.includes("rule outcome: <strong>"));
  assert.ok(page.includes('" (less severe than the rules: discarded)"'));
  assert.ok(read("../app/assessments/[id]/evidence/page.tsx").includes("That is not the same as\n            being safe."));
  assert.ok(read("../components/LoginForm.tsx").includes("Never\n        share your key; it is not shown again."));
});
