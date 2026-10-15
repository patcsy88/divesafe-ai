# ADR 0019: Asynchronous assessment creation

Status: accepted by the user on 2026-10-14 (option 3); reviewed by the security-reviewer and risk-reviewer (no safety blockers; requirements below); built in three stages (see Build status)

## Context

`POST /v1/assessments` runs the whole pipeline inside the request: connectors, then the agent stage
(up to the stage timeout, 60 s by default and longer for slow local models), then the store.

- The web UI waits at most 120 s (`API_TIMEOUT_MS`). A longer stage ends as "outcome unknown", even
  though the server may still store a record afterwards.
- `Idempotency-Key` replay is refused after 10 minutes (`REPLAY_WINDOW`), so a retry after a slow
  run can end in a 409.
- There is no server-wide cap on assessments in flight; the create limit is per actor per minute.
- A slow model run is measured at about 40 minutes for seven cases on a local 20B model, so a
  request that waits for the model will not suit larger stage budgets.

## Options

1. **Do nothing; raise the UI timeout.** Simple, but holds a connection per assessment, still ends
   badly on proxy timeouts, and does not fix the replay window.
2. **202 and poll, with the pending state kept in memory.** `POST` validates, returns `202` with the
   id and a `Location` to `GET /v1/assessments/{id}`; a background task runs the pipeline. While it
   runs, `GET` returns `{"status": "RUNNING"}`. Simple, but a restart or a second worker loses or
   cannot see the pending run, which conflicts with the multi-worker setup (Postgres, ADR 0006).
3. **202 and poll, with the pending state stored.** A `PENDING_RUN` row (id, actor, plan, created
   time, state `RUNNING`/`FAILED`) is written before the task starts. A worker that dies leaves a
   row that a later `GET` reports as `FAILED` after a lease expires. Needs a migration and a
   repository change, and the stored `AssessmentRecord` stays complete-only.

Recommendation: option 3, because the human gate and audit trail rely on durable state and the
service may run several workers. Option 2 is acceptable only for a single-worker development mode.

## Proposed decision (if option 3 is chosen)

- `POST` returns `202 Accepted` with `{id, status: "RUNNING"}` and `Location`. Validation errors
  (422, 404, 503) still return at once, before any work starts.
- The run is bounded: one overall deadline (the stage timeout plus connector time) and a
  server-wide cap on runs in flight; beyond the cap the POST returns `429` with `Retry-After`.
- A run that fails, times out or dies is reported as `FAILED` with a fixed message and **never** as a
  result. A failed run is not an INSUFFICIENT EVIDENCE assessment and offers no decision. (The
  pipeline already turns source and agent failures into INSUFFICIENT EVIDENCE inside a completed
  run; this is only for the run itself failing.)
- `GET` of a `RUNNING` or `FAILED` id returns status only, with no recommendation field, so nothing
  can be acted on early. The decision endpoint refuses an id that has no completed record (404 or
  409), so the human gate is unchanged.
- Idempotency: the same key returns the same id and its current status. The 10-minute replay window
  applies to completed records only; a `RUNNING` run is always joined, never started twice.
- The record's `created_at` stays the completion time as now; the decision-age clock is unchanged.
- The web UI polls with a back-off up to a limit, shows "running" and "failed" plainly, and still
  holds no risk logic.

## Decisions

1. **Option 3**: pending state is stored.
2. **A `RUNNING` row counts as `FAILED`** after the overall deadline plus a margin. The lease is a
   setting derived from the stage timeout, not a number in code.
3. **`FAILED` rows are kept 7 days**, then deleted (`DIVESAFE_FAILED_RUN_RETENTION_DAYS`). Cleanup
   runs at startup and periodically and touches only `FAILED` rows. Completed assessment records
   are never deleted by it.
4. **At most 4 runs in flight per server** (`DIVESAFE_MAX_RUNS_IN_FLIGHT`), counted from the stored
   `RUNNING` rows so the cap holds across workers. The count and the insert happen in one
   transaction. Beyond the cap, `POST` returns `429` with `Retry-After`; runs are not queued.
5. **The synchronous mode is dropped.** There is one path. Scripts poll the `Location` URL.

## Requirements from review (the implementation must meet all of them)

**Storage and completion**
1. A separate `pending_runs` table in a forward-only migration. `assessments` and its guard triggers
   are untouched. Columns: id, created_by, assigned_decider, plan, created_at, state, run_token,
   lease_expires_at, failed_at, failure_code. No raw Idempotency-Key, evidence, model text or
   exception text. The app role gets SELECT, INSERT, UPDATE, DELETE on this table only, and
   `verify(least_privilege=True)` checks it.
2. `repository.get` never returns a pending row. Pending runs have their own methods, in both the
   in-memory and Postgres repositories, under one shared contract test.
3. Completion is one transaction: store the record and delete the pending row only if the row is
   still `RUNNING`, holds this run's token and its lease has not expired. Otherwise the record is
   discarded and logged. `FAILED` is terminal for an id; a late run never turns it into a result.
4. The whole run (connectors and agent stage) sits under one deadline strictly shorter than the
   lease. `gather_evidence` has no overall timeout today, so one is added. The lease uses the
   database clock in Postgres and an injectable clock in memory.
5. Every way a run can fail ends as `FAILED` with a fixed failure code (`timeout`, `storage`,
   `internal`, `shutdown`, `start_passed`) and never as a result. This includes a planned start that
   passes before the run begins. A storage outage at completion leaves the row `RUNNING`, which
   reads as `FAILED` after the lease.
6. The run takes `now` when it actually starts and always passes the clock, so the result is judged
   again at completion (`assessment.window_started`, stale evidence). A long run can only make the
   result stricter.

**Admission**
7. One transaction under an advisory lock (Postgres) or the repository lock (memory): join check
   first, then the server-wide count, then a per-actor count, then the insert. Only `RUNNING` rows
   with an unexpired lease count. A per-actor cap (`DIVESAFE_MAX_RUNS_PER_ACTOR`) stops one
   key taking the whole server; polling is never needed to free a slot.
8. Joining by Idempotency-Key compares plan, assigned decider and creator and refuses any mismatch
   with 422. It happens before the cap, so a retry is never met with 429. Two concurrent POSTs with
   one key start exactly one run. A key whose run is `FAILED` gets 409 ("nothing was recorded; use a
   new key"), and the run is never restarted while its row exists. Cleanup deletes `FAILED` rows
   after the retention period (7 days by default); after that the same key starts a new run, so
   `FAILED` is terminal only for the retention period. After completion the current replay rules apply.
9. All input validation (site, plan, decider, start in the past, empty policy) finishes before 202.
10. A 429 starts with "nothing was recorded", carries `Retry-After` from settings, and still uses the
    per-actor create quota. New settings are bounded; the lease must exceed the stage timeout plus
    the connector deadline plus a margin.

**Reading status**
11. A `RunStatusView` (`extra="forbid"`, `status: Literal["RUNNING","FAILED"]`) carries id, status,
    a fixed note and the disclaimer, and nothing else. A test asserts it has no field for a record,
    recommendation, evidence, rule results, confidence or "safe".
12. Fixed notes. RUNNING: "No result yet. Nothing has been assessed. Do not act." FAILED: "The
    assessment did not complete. This is not a result and not INSUFFICIENT EVIDENCE: nothing was
    assessed. Start a new one."
13. Viewers may read any id, as now. A key without the viewer role may read only its own runs, and a
    completed record is returned with the decision and actual conditions removed, as `replay()` does.
    Any other id returns 404, so existence is not revealed.
14. `/evidence`, `/decision` and `/actual-conditions` on a pending or failed id record nothing and
    refuse. Where the caller may see the run the answer is 409 starting "nothing was recorded", so
    the web's refused flag fires; otherwise 404. `/evidence` never returns an empty view for them.

**Lifetime and errors**
15. Tasks live in an app-owned set, not Starlette `BackgroundTasks`. On shutdown: refuse new POSTs
    with 503, allow a bounded grace period, cancel, mark rows `FAILED` (`shutdown`) as far as
    possible, then close the provider. A cancelled run stores nothing.
16. The task wrapper catches `BaseException`, logs only run id, actor and exception type, and stores
    only the fixed code. Asyncio's unhandled-exception path is never reached.
17. Cleanup first marks expired `RUNNING` rows `FAILED`, then deletes `FAILED` rows past retention
    in bounded batches using a partial index, and is safe to run on several workers at once. It
    never touches `assessments`. Before deleting, it adds to a count by failure code, so failure
    rates by category survive retention.

**Web**
18. The UI treats the response as a union on `status` with an explicit allow-list. `RUNNING` and
    `FAILED` never use the recommendation badge, the pending-decision banner or the decision form,
    and an unknown status is an error screen. Today an unrecognised value falls through to the
    INSUFFICIENT EVIDENCE badge and `status !== "DECIDED"` shows the pending banner, so both must
    change. If polling stops at its limit the UI says "No answer yet; outcome unknown".

**Tests**
19. The synchronous-create safety tests are moved to create, poll, assert. None is deleted or
    weakened, including the window-start-during-request test, which gets a background equivalent.
20. New tests cover: each failure path in item 5; a late completion after the lease (mutation-check
    by removing the conditional store); concurrent admission at the cap; join cases in item 8; a
    cancelled run storing nothing; cleanup unable to touch `assessments`; the window passing and the
    evidence going stale during a run.

## Choices made while folding in the reviews

- The security-reviewer proposed 404 and the risk-reviewer 409 for the decision endpoint on a pending
  id. Item 14 uses 409 where the caller may see the run and 404 where not.
- Idempotent ids stay `sha256(actor, key)`. A weak key exposes an id to viewers who know the actor
  name, so the docs require random keys of at least 128 bits (the web UI already makes them).
  An HMAC with a server secret is possible later.
- Making production refuse to start without `DIVESAFE_DECISION_MAX_AGE_MINUTES` is raised, not
  decided: the value must come from the owner.

## Build status

- **Stage 1 (storage): built.** Migration 2 adds `pending_runs` and `run_failure_counts`.
  `RunStore` (`services/runs.py`) is implemented by the in-memory and PostgreSQL repositories and
  both pass `tests/unit/test_run_store_contract.py`. Covered: admission under a lock with the
  server and per-actor caps, joins that compare plan, decider and creator, a lease, a
  compare-and-set completion (a late, failed or wrong-token run stores nothing), bounded cleanup
  with failure counts, and the least-privilege checks in `verify`. The PostgreSQL tests ran
  locally against a throwaway container and in CI.
- **Stage 2 (API and runner): built.** `POST` returns 202 with `Location`; `RunManager`
  (`api/runner.py`) owns the tasks, applies the run deadline, completes by compare-and-set and
  stores only a fixed failure code. Shutdown refuses new runs, waits a grace period, cancels and
  marks runs failed, then closes the provider. `GET` returns `RunStatusView` while a run is
  pending or failed. A key without the viewer role reads only its own runs and sees a finished
  record without the decision. `gather_evidence` has an overall timeout. Tests:
  `tests/safety/test_api_async_runs.py`; the older create tests now create, then poll.
- **Stage 3 (web): built.** `web/lib/status.ts` classifies every reply with an explicit allow-list
  (`PENDING_HUMAN`, `DECIDED`, `RUNNING`, `FAILED`); an unknown status, a status that disagrees with
  the decision, or a run that carries a result is an error screen. A create takes only the id from
  the reply. A run has its own screen with no badge, pending banner or forms, and
  `RunProgress` refreshes the page with a back-off from 3 s to 15 s and stops after 6 minutes with "No answer yet;
  outcome unknown". Tests: `web/tests/status.test.ts`. The web call timeout is now 30 s.
- **Follow-ups after review (built):**
  - A key without the viewer role is told that decisions are not shown to it, not that the record
    is pending: `AssessmentView.decision_withheld` is true and `status_note` is fixed text. The web
    allow-list requires the flag, refuses a withheld reply that carries a decision, and shows
    neither the pending banner nor the decision or conditions forms.
  - `StartPassedError` and `SiteMismatchError` are separate subclasses of `InvalidPlanError`; only
    the first is stored as `start_passed`. A site mismatch is `internal`.
  - `CONNECTOR_BUDGET_SECONDS` is the single source of the connector budget. `assess_dive` takes
    `connector_timeout_seconds` and passes it to `gather_evidence`. `create_app` refuses to start if
    the run deadline is shorter than the provider's own stage timeout plus the connector budget and
    margin, which covers a provider whose budget differs from the settings.
  - API tests: an unexpected error ends the run `FAILED`/`internal`, stores nothing, logs only the
    exception type and reaches no asyncio handler; evidence that goes stale during a run is judged
    again and no earlier result is dropped (requirement 20).
  - `PostgresAssessmentRepository.verify` also reports an application role that owns
    `run_failure_counts`.
  - Every API string on the assessment page goes through `plain()`, guarded by a source test.
    The conflict handling in `failed()` and the polling back-off moved into `web/lib/conflict.ts`
    and `web/lib/polling.ts`, where they are tested directly.
- **Review round 2 (built):**
  - A key without the viewer role gets `status: "DECISION_WITHHELD"` and
    `overrides_to_less_severe: null`, so no field says whether a decision exists. A test asserts the
    reply is identical before and after a decision. The web allow-list requires the status, the
    flag and the null override to agree, and refuses a withheld reply carrying a decision or later
    conditions. Older web builds refuse the new status and show an error screen. The Evidence link
    is hidden for withheld keys, which cannot read evidence.
  - `PostgresAssessmentRepository.verify` counts membership in the owning role as ownership for
    all three tables, flags an application role that owns or is a member of the owner of a guard
    function, checks each guard trigger calls its own function, and reports a missing run table as
    a problem (tables are passed as oids, so a missing table is no longer an opaque outage).
  - `web/tests/plain.test.ts` scans every page and component and fails on any printed value that
    is not wrapped in `plain()` or on a short list of named fixed-text expressions. `ApiError`
    strips control and bidi characters from every message. This covers the evidence, sites and
    home pages and the site name in the form.
  - Mutation checks were run for every test added in this round and the earlier follow-ups.
- Not yet covered: a run that dies with its process is marked failed only when its lease expires
  and something reads it or cleanup runs; cleanup runs every 5 minutes in each worker.
- The failure counts table is written by cleanup only; nothing reads it yet.

## Not solved

- Cancelling a run. Worker threads for model calls cannot be cancelled (ADR 0016).
- Notifying the user when a run ends; the UI only polls.
- The web page stops polling after 6 minutes, fixed in code, while the run deadline can be set up
  to 7200 s. A run longer than that finishes unseen; reloading the page shows it.
- `verify()` does not compare guard function bodies; a function the application role does not own
  but the owner later replaced is not detected. The application role can also delete `FAILED` rows
  before they are counted. These are operational counts, not audit data.
- `connector_timeout_seconds` is not range-checked; the only caller passes the constant.
- The application role can still reset `run_failure_counts` rows it is allowed to update. Nothing
  reads the counts yet.
- Rendering of `RunProgress` itself is checked by reading its source, not by a renderer; the web
  package has no browser or React test tooling.
- Reviewers: this changes the API, persistence and the web flow, so it needs the security-reviewer
  and the risk-reviewer before it is built.
