import assert from "node:assert/strict";
import { test } from "node:test";
import { describeFailure } from "../lib/api.ts";
import { badgeClass, labelFor, plain } from "../lib/present.ts";
import { parsePositive, toAwareIso } from "../lib/time.ts";

test("a local time gets an explicit zone and a bare one is refused", () => {
  assert.equal(toAwareIso("2026-10-12T09:30", "+08:00"), "2026-10-12T09:30:00+08:00");
  assert.equal(toAwareIso("2026-10-12T09:30", "UTC"), "2026-10-12T09:30:00Z");
  for (const bad of ["", "2026-10-12", "2026-10-12T09:30:00", "tomorrow", "2026-13-45T25:61"]) {
    assert.equal(toAwareIso(bad, "UTC"), null, bad);
  }
  assert.equal(toAwareIso("2026-10-12T09:30", ""), null);
  assert.equal(toAwareIso("2026-10-12T09:30", "+05:00"), null);
});

test("numbers must be positive, and whole where required", () => {
  assert.equal(parsePositive("45", true), 45);
  assert.equal(parsePositive("18.5", false), 18.5);
  for (const bad of ["", "0", "-5", "1e3", "abc", "NaN", "Infinity", "4.5", " "]) {
    assert.equal(parsePositive(bad, true), null, bad);
  }
  assert.equal(parsePositive("-1", false), null);
});

test("an unknown recommendation is shown as inconclusive, never as safe", () => {
  assert.equal(labelFor("GO"), "GO");
  assert.equal(labelFor("go"), "INSUFFICIENT EVIDENCE");
  assert.equal(labelFor("SAFE"), "INSUFFICIENT EVIDENCE");
  assert.equal(labelFor(""), "INSUFFICIENT EVIDENCE");
  assert.equal(badgeClass("SAFE"), "badge insufficient");
  assert.equal(badgeClass("NO-GO"), "badge nogo");
});

test("failure messages never echo the server's body", () => {
  assert.match(describeFailure(401, "secret-detail", null), /not accept/);
  assert.ok(!describeFailure(500, "SECRET-TRACE", null).includes("SECRET"));
  assert.ok(!describeFailure(403, "SECRET", null).includes("SECRET"));
  assert.match(describeFailure(429, null, "12"), /12/);
  assert.match(describeFailure(422, [{ loc: ["body", "x"], msg: "bad" }], null), /body\.x: bad/);
  assert.ok(describeFailure(422, "y".repeat(5000), null).length <= 300);
});

test("third-party text loses control and bidi characters but keeps newlines", () => {
  assert.equal(plain("a\u202Eb\u0000c\nd"), "abc\nd");
});

test("an unknown outcome on a failed write is not reported as nothing recorded", () => {
  for (const status of [0, 500, 502, 504]) {
    assert.ok(!/Nothing was recorded/.test(describeFailure(status, null, null)), String(status));
  }
  assert.match(describeFailure(503, null, null), /Nothing was recorded/);
});

test("a warning or knowledge item is never called measured", async () => {
  const { provenanceLabel, isLessSevere } = await import("../lib/present.ts");
  assert.equal(provenanceLabel("observation"), "measured");
  for (const kind of ["forecast", "model", "prediction"]) assert.match(provenanceLabel(kind), /not measured/);
  for (const kind of ["notice", "knowledge", "", "surprise"]) assert.ok(provenanceLabel(kind) !== "measured", kind);
  assert.ok(isLessSevere("GO", "NO-GO") && isLessSevere("CAUTION", "INSUFFICIENT EVIDENCE"));
  assert.ok(!isLessSevere("NO-GO", "GO") && !isLessSevere("GO", "GO"));
  assert.ok(!isLessSevere("GO", "SAFE") && !isLessSevere("", "NO-GO"));
});

test("echoed failure text is stripped of bidi characters", () => {
  assert.ok(!describeFailure(409, "a\u202Eb", null).includes("\u202E"));
  assert.ok(!describeFailure(422, "a\u202Eb", null).includes("\u202E"));
  assert.ok(!describeFailure(422, [{ loc: ["x\u202E"], msg: "m\u202E" }], null).includes("\u202E"));
});
