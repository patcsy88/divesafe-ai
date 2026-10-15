import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { ApiError } from "../lib/api.ts";
import { resolveConflict } from "../lib/conflict.ts";
import { sameObservations } from "../lib/observations.ts";
import { DELAYS_MS, delayFor, GIVE_UP_AFTER_MS } from "../lib/polling.ts";
import type { Fetched } from "../lib/status.ts";
import type { AssessmentView } from "../lib/types.ts";

const refusal = new ApiError(409, "nothing was recorded; refused");
const view = (human_decision: unknown): Fetched =>
  ({ kind: "assessment", view: { record: { human_decision } } }) as unknown as Fetched;
const run: Fetched = { kind: "run", view: { id: "a", status: "RUNNING" } } as unknown as Fetched;
const decided = (v: AssessmentView) => v.record.human_decision !== null;

test("a refusal is followed to the record only when a fresh read shows what was submitted", async () => {
  const seen = await resolveConflict(refusal, async () => view({ decision: "NO-GO" }), decided);
  assert.deepEqual(seen, { kind: "record" });
  const not = await resolveConflict(refusal, async () => view(null), decided);
  assert.deepEqual(not, { kind: "error", error: refusal.message });
});

test("a run with no record has nothing recorded, so the refusal stands", async () => {
  const outcome = await resolveConflict(refusal, async () => run, () => true);
  assert.deepEqual(outcome, { kind: "error", error: refusal.message });
});

test("a different decision already recorded is reported and the key is renewed", async () => {
  const outcome = await resolveConflict(
    refusal,
    async () => view({ decision: "CAUTION" }),
    () => false,
    (v) => `Someone else decided: ${v.record.human_decision?.decision}. Yours was not recorded.`,
  );
  assert.equal(outcome.kind, "error");
  if (outcome.kind === "error") {
    assert.match(outcome.error, /decided: CAUTION. Yours was not recorded/);
    assert.equal(outcome.renewKey, true);
  }
});

test("a 401 on the re-read ends the session; another API error keeps the original refusal", async () => {
  const expired = await resolveConflict(refusal, async () => { throw new ApiError(401, "no"); }, decided);
  assert.deepEqual(expired, { kind: "login" });
  const down = await resolveConflict(refusal, async () => { throw new ApiError(503, "down"); }, decided);
  assert.deepEqual(down, { kind: "error", error: refusal.message });
});

test("a failure that is not an API error is not swallowed", async () => {
  await assert.rejects(resolveConflict(refusal, async () => { throw new TypeError("bug"); }, decided), TypeError);
});

test("the polling back-off grows to a cap and gives up at six minutes", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 6, 100].map(delayFor), [3000, 3000, 5000, 5000, 10000, 15000, 15000, 15000]);
  assert.deepEqual([-1, NaN, 1.9].map(delayFor), [3000, 3000, 3000]);
  assert.equal(GIVE_UP_AFTER_MS, 360_000);
  assert.ok(DELAYS_MS.every((d, i) => i === 0 || d >= (DELAYS_MS[i - 1] ?? 0)));
});

test("the run component uses the shared back-off and says the outcome is unknown when it gives up", () => {
  const source = readFileSync(fileURLToPath(new URL("../components/RunProgress.tsx", import.meta.url)), "utf8");
  assert.ok(source.includes("delayFor(round)") && source.includes("GIVE_UP_AFTER_MS"));
  assert.ok(source.includes("outcome unknown") && source.includes("Nothing may be acted on"));
  assert.ok(source.includes("document.hidden") && source.includes("if (gaveUp || busy) return;"));
  assert.ok(!/\bapi\b.*fetch\(|fetch\(/.test(source), "the component never calls the API itself");
});

test("different conditions already on record are reported and are not taken for the submission", async () => {
  const stored = (observations: Record<string, unknown>): Fetched =>
    ({ kind: "assessment", view: { record: { human_decision: { decision: "GO" }, actual_conditions: { observations } } } }) as unknown as Fetched;
  const same = (v: AssessmentView) => sameObservations(v.record.actual_conditions?.observations, { wave_height_m: 1, notes: "calm" });
  const differs = (v: AssessmentView) => (v.record.actual_conditions ? "Different conditions were already reported." : null);
  const ok = await resolveConflict(refusal, async () => stored({ wave_height_m: 1, notes: "calm" }), same, differs);
  assert.deepEqual(ok, { kind: "record" });
  const other = await resolveConflict(refusal, async () => stored({ wave_height_m: 3, notes: "calm" }), same, differs);
  assert.deepEqual(other, { kind: "error", error: "Different conditions were already reported.", renewKey: true });
  const none = await resolveConflict(refusal, async () => view({ decision: "GO" }), same, differs);
  assert.deepEqual(none, { kind: "error", error: refusal.message });
});

test("observations match only when every name and value is the same", () => {
  const sub = { wave_height_m: 1, notes: "calm" };
  assert.equal(sameObservations({ wave_height_m: 1, notes: "calm" }, sub), true);
  assert.equal(sameObservations({ wave_height_m: 2, notes: "calm" }, sub), false);
  assert.equal(sameObservations({ wave_height_m: 1 }, sub), false);
  assert.equal(sameObservations({ wave_height_m: 1, notes: "calm", extra: 1 }, sub), false);
  assert.equal(sameObservations({ wave_height_m: 1, other: "calm" }, sub), false);
  assert.equal(sameObservations({ wave_height_m: "1", notes: "calm" }, sub), false);
  assert.equal(sameObservations(undefined, sub), false);
  assert.equal(sameObservations({}, {}), true);
});

test("the conditions form follows a refusal to the record only when the observations match", () => {
  const source = readFileSync(fileURLToPath(new URL("../app/actions.ts", import.meta.url)), "utf8");
  assert.ok(source.includes("sameObservations(v.record.actual_conditions?.observations, parsed.observations)"));
  assert.ok(!source.includes("v.record.actual_conditions !== null"));
});
