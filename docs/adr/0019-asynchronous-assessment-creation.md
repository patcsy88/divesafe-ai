# ADR 0019: Asynchronous assessment creation

Status: accepted by the user on 2026-10-14 (option 3); not yet reviewed by the security-reviewer and risk-reviewer, and nothing is built

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

## Not solved

- Cancelling a run. Worker threads for model calls cannot be cancelled (ADR 0016).
- Notifying the user when a run ends; the UI only polls.
- Reviewers: this changes the API, persistence and the web flow, so it needs the security-reviewer
  and the risk-reviewer before it is built.
