# ADR 0019: Asynchronous assessment creation

Status: proposed (for the user to decide; nothing is built)

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

## Open questions for the user

1. Option 2 or 3?
2. How long is a `RUNNING` row kept before it counts as `FAILED` (suggest the overall deadline plus
   a margin)?
3. How long are `FAILED` rows kept?
4. What cap on runs in flight per server?
5. Should the existing synchronous behaviour stay as an option (`Prefer: wait`) for scripts?

## Not solved

- Cancelling a run. Worker threads for model calls cannot be cancelled (ADR 0016).
- Notifying the user when a run ends; the UI only polls.
- Reviewers: this changes the API, persistence and the web flow, so it needs the security-reviewer
  and the risk-reviewer before it is built.
