import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { RECOMMENDATIONS } from "../lib/present.ts";

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
const form = read("../components/DecisionForm.tsx");

test("the decision select starts on an empty, disabled choice and is required", () => {
  const select = form.slice(form.indexOf("<select"), form.indexOf("</select>"));
  assert.ok(select.length > 0, "no select");
  assert.match(select, /\n\s*required\n/);
  const options = [...select.matchAll(/<option\b[^>]*>/g)].map((m) => m[0]);
  assert.equal(options[0], '<option value="" disabled>');
  assert.ok(select.includes('<option value="" disabled>Choose…</option>'));
  assert.equal(options.filter((o) => o.includes('value=""')).length, 1);
  // The select is controlled, so what it shows first is the initial choice: nothing pre-selected.
  const empty = 'const [choice, setChoice] = useState("");';
  assert.ok(form.includes(empty), "the choice must start empty");
  assert.equal(form.match(/\bsetChoice\(/g)?.length, 1, "the choice is set only by the select");
  assert.ok(form.includes("onChange={(e) => setChoice(e.target.value)}"));
});

test("the select shows the choice again after the action returns, and nothing else sets it", () => {
  // React resets the form after each action; without this the select shows "Choose…" while the
  // confirm group still names the earlier choice (found 2026-11-23). The form sends the select's
  // DOM value, so the effect may write only `choice` there, and only the select sets `choice`.
  assert.match(form, /ref=\{select\}/);
  const effect =
    "  useEffect(() => {\n" +
    "    if (select.current && select.current.value !== choice) select.current.value = choice;\n" +
    "  }, [state, choice]);";
  assert.ok(form.includes(effect), "the select is not set back to the choice after each action");
  const write = /\.(?:value|selectedIndex|selected|defaultValue)\s*=(?!=)[^;\n]*/g;
  const writes = [...form.matchAll(write)].map((m) => m[0]);
  assert.deepEqual(writes, [".value = choice"], "only the choice is written to the select");
  assert.doesNotMatch(form, /setAttribute\(|defaultValue=/, "the select is set another way");
  assert.equal(form.match(/\bsetChoice\b/g)?.length, 2, "setChoice is passed or aliased");
});

test("the server action refuses a missing or unknown decision before calling the API", () => {
  const actions = read("../app/actions.ts");
  const start = actions.indexOf("export async function submitDecision");
  assert.ok(start >= 0, "no submitDecision");
  const next = actions.indexOf("\nexport ", start + 1);
  const body = actions.slice(start, next < 0 ? undefined : next);
  const guard = body.indexOf(
    "if (!(RECOMMENDATIONS as readonly string[]).includes(decision)) {\n" +
      '    return { error: "Choose the decision you are making." };',
  );
  assert.ok(guard > 0, "no refusal of a missing decision");
  const readAt = body.indexOf('const decision = text(data, "decision");');
  assert.ok(readAt >= 0 && readAt < guard, "the decision must be read before the refusal");
  const call = body.indexOf("await recordDecision(");
  assert.equal(body.split("recordDecision(").length, 2, "the API is called once");
  assert.ok(call > guard, "the refusal must come before the API call, inside submitDecision");
  const sent =
    "await recordDecision(\n      key,\n      id,\n" +
    "      { decision, ...(rationale ? { rationale } : {}) },";
  assert.equal(body.indexOf(sent), call, "the API is sent the decision that was checked");
  assert.ok(!(RECOMMENDATIONS as readonly string[]).includes(""));
});
