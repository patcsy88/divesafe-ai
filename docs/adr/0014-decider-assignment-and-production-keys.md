# ADR 0014: Decider assignment and production keys

Status: accepted

## Decision

- `AssessmentRecord` gains `created_by` and `assigned_decider`, both set by the server.
- `POST /v1/assessments` takes an optional `decider`. It defaults to the requester when they hold
  the `decider` role. The named person must be a known key holder with that role, else 422.
- The stored name is the configured key name. Only the assigned decider (case-insensitive) may record the decision or actual conditions. Anyone
  else gets 403 before any other check, and nothing is recorded, so a wrong decider cannot lock the assessment.
- The assignment cannot be changed after creation and is not accepted in decision bodies.
- With `environment=production` and API-key auth, start-up is refused when no keys are configured.

## Not solved

- A record with no assignee can be decided by nobody (fail closed); none exist in a deployment yet.
- Decider names are visible to viewers and assessors, and probing names at create time is possible.
- Dev authentication accepts any name, so ownership is not real outside production.
- Names are compared with `casefold` only, with no Unicode normalisation.
- Production does not require a key with the decider role; without one every create returns 422.
- An actor who is both assessor and decider can still assign and approve their own assessment.
- Reassignment (for example when the decider is ill) is not supported; a new assessment is needed.
- Keys are configured at start-up, so a removed decider's assessments stay assigned to them.
- Reads are not scoped to owner.
- `assigned_decider` is stored in the JSON record; the PostgreSQL trigger treats it as immutable
  because it only allows `human_decision` and `actual_conditions` to change.
