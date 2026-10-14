import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, test } from "node:test";
import { fileURLToPath } from "node:url";
import { createAssessment, getAssessment } from "../lib/api.ts";
import { classify, failureText } from "../lib/status.ts";

const ID = "a".repeat(32);
const run = (over: Record<string, unknown> = {}) => ({
  id: ID,
  status: "RUNNING",
  created_at: "2026-10-10T00:00:00Z",
  failure_code: null,
  status_note: "No result yet.",
  notice: "n",
  ...over,
});
const record = (over: Record<string, unknown> = {}) => ({
  id: ID,
  final_recommendation: "NO-GO",
  human_decision: null,
  ...over,
});
const finished = (status: string, rec: Record<string, unknown> = record()) => ({ status, record: rec });
const DECIDED_REC = record({ human_decision: { decision: "NO-GO" } });

const real = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = real;
});
const reply = (body: unknown, status = 200) => {
  globalThis.fetch = (async () => new Response(JSON.stringify(body), { status })) as typeof fetch;
};

test("a running or failed run is a run, never an assessment", () => {
  assert.equal(classify(run())?.kind, "run");
  assert.equal(classify(run({ status: "FAILED", failure_code: "timeout" }))?.kind, "run");
});

test("a finished assessment is accepted only when its status matches its decision", () => {
  assert.equal(classify(finished("PENDING_HUMAN"))?.kind, "assessment");
  assert.equal(classify(finished("DECIDED", DECIDED_REC))?.kind, "assessment");
  assert.equal(classify(finished("DECIDED")), null);
  assert.equal(classify(finished("PENDING_HUMAN", DECIDED_REC)), null);
});

test("an unknown status is refused, not shown as pending or as a result", () => {
  for (const status of ["COMPLETE", "running", "", "DONE", null, 7, undefined]) {
    assert.equal(classify({ ...finished("PENDING_HUMAN"), status }), null, String(status));
    assert.equal(classify(run({ status })), null, String(status));
  }
});

test("a run that carries a result is refused", () => {
  assert.equal(classify(run({ record: record() })), null);
  assert.equal(classify(run({ final_recommendation: "GO" })), null);
  assert.equal(classify(run({ recommendation: "GO" })), null);
});

test("malformed bodies are refused", () => {
  for (const body of [null, undefined, "x", 3, [], {}, { status: "RUNNING" }, { status: "DECIDED" }]) {
    assert.equal(classify(body), null);
  }
  assert.equal(classify(finished("PENDING_HUMAN", { id: ID })), null);
  assert.equal(classify(finished("PENDING_HUMAN", record({ id: 5 }))), null);
});

test("a create takes only the id, from a run or from a replayed record", async () => {
  reply(run(), 202);
  assert.equal(await createAssessment("k-12345678", body), ID);
  reply(finished("PENDING_HUMAN"), 200);
  assert.equal(await createAssessment("k-12345678", body), ID);
});

test("a create reply that is not a known shape, or has a bad id, is an error", async () => {
  for (const bad of [{}, run({ status: "COMPLETE" }), run({ id: "../x" }), finished("DECIDED")]) {
    reply(bad, 202);
    await assert.rejects(() => createAssessment("k-12345678", body), /does not recognise/);
  }
});

test("reading an assessment with an unrecognised reply fails instead of rendering a result", async () => {
  reply({ status: "SOMETHING_NEW", record: record() });
  await assert.rejects(() => getAssessment("k-12345678", ID), /does not recognise/);
});

test("failure codes have fixed wording, and an unknown code is not echoed", () => {
  for (const code of ["timeout", "storage", "internal", "shutdown", "start_passed"]) {
    assert.ok(failureText(code).length > 0 && failureText(code) !== failureText(null));
  }
  assert.equal(failureText("<script>alert(1)</script>"), failureText(null));
  assert.ok(!failureText("<script>").includes("script"));
});

test("the page keeps a run apart from the badge, the pending banner and the forms", () => {
  const page = readFileSync(
    fileURLToPath(new URL("../app/assessments/[id]/page.tsx", import.meta.url)),
    "utf8",
  );
  const runBranch = page.slice(page.indexOf('fetched.kind === "run"'), page.indexOf("const view = fetched.view"));
  assert.ok(runBranch.length > 0);
  for (const forbidden of ["badgeClass", "DecisionForm", "ConditionsForm", "PENDING HUMAN", "labelFor"]) {
    assert.ok(!runBranch.includes(forbidden), `the run screen uses ${forbidden}`);
  }
  assert.ok(!/status !== "DECIDED"/.test(page), "an unknown status must not fall into pending");
  assert.ok(runBranch.includes("NO RESULT."), "the run screen must say there is no result");
  assert.ok(runBranch.includes("not INSUFFICIENT EVIDENCE"), "a failed run must not read as an outcome");
  assert.ok(runBranch.includes("<RunProgress />"), "a running run must keep checking");
});

const body = {
  site_id: "s",
  planned_start: "2026-10-10T08:00:00+08:00",
  planned_duration_minutes: 40,
  max_depth_m: 18,
};
