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
  deterministic_recommendation: "NO-GO",
  proposed_recommendation: null,
  human_decision: null,
  actual_conditions: null,
  ...over,
});
const finished = (
  status: string,
  rec: Record<string, unknown> = record(),
  withheld: unknown = status === "DECISION_WITHHELD",
) => ({
  status,
  record: rec,
  decision_withheld: withheld,
  overrides_to_less_severe: status === "DECISION_WITHHELD" ? null : false,
});
const DECISION = { decision: "NO-GO", decided_by: "alice", decided_at: "2026-10-10T00:00:00Z" };
const DECIDED_REC = record({ human_decision: DECISION });

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

test("a reply must say whether the decision was withheld, and a withheld one carries no decision", () => {
  const { decision_withheld: _unused, ...missing } = finished("PENDING_HUMAN");
  assert.equal(classify(missing), null);
  assert.equal(classify({ ...finished("PENDING_HUMAN"), decision_withheld: undefined }), null);
  for (const bad of ["false", 0, null]) {
    assert.equal(classify(finished("PENDING_HUMAN", record(), bad)), null, String(bad));
  }
  assert.equal(classify(finished("DECISION_WITHHELD"))?.kind, "assessment");
  assert.equal(classify(finished("DECISION_WITHHELD", DECIDED_REC)), null);
  const actual = record({ actual_conditions: { reported_by: "x", reported_at: "t", observations: {} } });
  assert.equal(classify(finished("DECISION_WITHHELD", actual)), null);
});

test("the status and the withheld flag must agree, and a withheld view has no override flag", () => {
  assert.equal(classify(finished("PENDING_HUMAN", record(), true)), null);
  assert.equal(classify(finished("DECIDED", DECIDED_REC, true)), null);
  assert.equal(classify(finished("DECISION_WITHHELD", record(), false)), null);
  assert.equal(classify({ ...finished("DECISION_WITHHELD"), overrides_to_less_severe: false }), null);
  assert.equal(classify({ ...finished("PENDING_HUMAN"), overrides_to_less_severe: null }), null);
  assert.equal(classify({ ...finished("PENDING_HUMAN"), overrides_to_less_severe: undefined }), null);
});

test("the page does not call a withheld decision pending or offer the forms for it", () => {
  const source = readFileSync(fileURLToPath(new URL("../app/assessments/[id]/page.tsx", import.meta.url)), "utf8");
  assert.ok(source.includes('view.status === "PENDING_HUMAN" && !withheld'));
  assert.ok(source.includes("!pending && !withheld && !r.actual_conditions"));
  assert.ok(source.includes("DECISION NOT SHOWN TO THIS KEY"));
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

test("a recommendation outside the four known values is refused, never shown as inconclusive", () => {
  for (const bad of ["SAFE", "go", "NO_GO", "", null, 3]) {
    assert.equal(classify(finished("PENDING_HUMAN", record({ final_recommendation: bad }))), null, String(bad));
    assert.equal(classify(finished("PENDING_HUMAN", record({ deterministic_recommendation: bad }))), null, String(bad));
  }
  assert.equal(classify(finished("PENDING_HUMAN", record({ proposed_recommendation: "SAFE" }))), null);
  assert.equal(classify(finished("PENDING_HUMAN", record({ proposed_recommendation: "CAUTION" })))?.kind, "assessment");
});

test("a decision that is not a real decision cannot make a record look decided or undecided", () => {
  for (const bad of [false, 0, "", {}, true, [], { ...DECISION, decision: "SAFE" }, { ...DECISION, decided_by: 1 }]) {
    assert.equal(classify(finished("DECIDED", record({ human_decision: bad }))), null, JSON.stringify(bad));
    assert.equal(classify(finished("PENDING_HUMAN", record({ human_decision: bad }))), null, JSON.stringify(bad));
  }
  assert.equal(classify(finished("PENDING_HUMAN", record({ human_decision: undefined }))), null);
});

test("actual conditions must be absent or an object", () => {
  assert.equal(classify(finished("PENDING_HUMAN", record({ actual_conditions: "x" }))), null);
  assert.equal(classify(finished("PENDING_HUMAN", record({ actual_conditions: false }))), null);
});

test("a run with a field the API does not send, or a field of the wrong type, is refused", () => {
  assert.equal(classify(run({ extra: 1 })), null);
  assert.equal(classify(run({ notice: 5 })), null);
  assert.equal(classify(run({ status_note: null })), null);
  assert.equal(classify(run({ failure_code: 5 })), null);
  assert.equal(classify(run({ failure_code: "timeout" })), null); // RUNNING has no failure code
  assert.equal(classify(run({ status: "FAILED", failure_code: null }))?.kind, "run");
});

test("a reply about another assessment is never shown under this address", async () => {
  reply(run({ id: "b".repeat(32) }));
  await assert.rejects(() => getAssessment("k-12345678", ID), /does not recognise/);
  reply(finished("PENDING_HUMAN", record({ id: "b".repeat(32) })));
  await assert.rejects(() => getAssessment("k-12345678", ID), /does not recognise/);
  reply(run());
  assert.equal((await getAssessment("k-12345678", ID)).kind, "run");
});

test("a 503 on a read does not claim that nothing was recorded", async () => {
  reply({ detail: "x" }, 503);
  await assert.rejects(
    () => getAssessment("k-12345678", ID),
    (error: Error) => /outcome is unknown/.test(error.message) && !/Nothing was recorded/i.test(error.message),
  );
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
  for (const inherited of ["__proto__", "toString", "constructor", "hasOwnProperty"]) {
    assert.equal(failureText(inherited), failureText(null), inherited);
  }
});

test("the page keeps a run apart from the badge, the pending banner and the forms", () => {
  const page = readFileSync(
    fileURLToPath(new URL("../app/assessments/[id]/page.tsx", import.meta.url)),
    "utf8",
  );
  const runBranch = page
    .slice(page.indexOf('fetched.kind === "run"'), page.indexOf("const view = fetched.view"))
    .replace(/\s+/g, " ");
  assert.ok(runBranch.length > 0);
  for (const forbidden of ["badgeClass", "DecisionForm", "ConditionsForm", "PENDING HUMAN", "labelFor"]) {
    assert.ok(!runBranch.includes(forbidden), `the run screen uses ${forbidden}`);
  }
  assert.ok(!/status !== "DECIDED"/.test(page), "an unknown status must not fall into pending");
  assert.ok(runBranch.includes("NO RESULT."), "the run screen must say there is no result");
  assert.ok(runBranch.includes("NO RESULT YET."), "the running screen must say there is no result");
  assert.equal((runBranch.match(/Do not act on this page/g) ?? []).length, 2);
  assert.ok(runBranch.includes("INSUFFICIENT EVIDENCE either"), "a failed run must not read as an outcome");
  assert.ok(runBranch.includes("does not mean conditions are acceptable"));
  assert.ok(!runBranch.includes("status_note"), "the run screen uses fixed wording, not the server's");
  assert.ok(runBranch.includes("<RunProgress />"), "a running run must keep checking");
});

const body = {
  site_id: "s",
  planned_start: "2026-10-10T08:00:00+08:00",
  planned_duration_minutes: 40,
  max_depth_m: 18,
};
