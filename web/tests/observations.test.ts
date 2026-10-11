import assert from "node:assert/strict";
import { test } from "node:test";
import { describeFailure } from "../lib/api.ts";
import { parseObservations } from "../lib/observations.ts";

const from = (fields: Record<string, string>) => parseObservations((n) => fields[n] ?? "");

test("filled numbers and notes become observations; empty boxes are left out", () => {
  const r = from({ wave_height_m: "1.5", visibility_m: " 12 ", notes: " calm " });
  assert.deepEqual(r, { ok: true, observations: { wave_height_m: 1.5, visibility_m: 12, notes: "calm" } });
});

test("nothing entered is refused rather than sent as an empty report", () => {
  assert.equal(from({}).ok, false);
  assert.equal(from({ notes: "   " }).ok, false);
});

test("non-numeric, negative, exponent and non-finite values are refused", () => {
  for (const bad of ["abc", "-1", "1e3", "NaN", "Infinity", "1,5", "0x10"]) {
    assert.equal(from({ current_knots: bad }).ok, false, bad);
  }
});

test("control characters and oversize notes are refused", () => {
  assert.equal(from({ notes: "a\u0000b" }).ok, false);
  assert.equal(from({ notes: "x".repeat(2001) }).ok, false);
  assert.equal(from({ notes: "line1\nline2" }).ok, true);
});

test("unknown field names cannot be smuggled in", () => {
  const r = parseObservations((n) => (n === "wave_height_m" ? "1" : n === "decided_by" ? "mallory" : ""));
  assert.ok(r.ok && !("decided_by" in r.observations));
});

test("a conflict explains itself; a forbidden decision explains the assignment", () => {
  assert.match(describeFailure(409, "this assessment already has a decision", null), /already has a decision/);
  assert.match(describeFailure(409, { x: 1 }, null), /current state/);
  assert.match(describeFailure(403, null, null), /assigned/);
  assert.ok(describeFailure(409, "y".repeat(5000), null).length <= 300);
});

test("sea temperature may be below zero; other values may not", () => {
  assert.ok(from({ water_temperature_c: "-1.5" }).ok);
  assert.equal(from({ wave_height_m: "-1.5" }).ok, false);
});

test("bidi and C1 control characters in notes are refused", () => {
  assert.equal(from({ notes: "a\u202Eb" }).ok, false);
  assert.equal(from({ notes: "a\u0085b" }).ok, false);
});
