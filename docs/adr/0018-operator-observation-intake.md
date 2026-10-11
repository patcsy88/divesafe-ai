# ADR 0018: Dive operator observation intake

Status: proposed (not built; needs the owner's decisions below)

## Context

No public feed gives underwater visibility or reef-level current for Pulau Tioman. Models and
agency forecasts describe surrounding waters, not the dive site. A dive master on site is the only
source of site-level conditions. This is a human observation, so it must be recorded as one: named,
timestamped, attributable, and never mistaken for a measurement by an instrument or for a forecast.

## Decision (proposed)

- **Evidence, not a rule input by itself.** An accepted report becomes an `EvidenceItem` with
  `data_kind=observation`, `is_forecast=False`, source `operator-observation`, the observer's
  configured key name in the provenance, and a short validity window (`valid_until`).
  Categories used: `currents`, `historical_observations`, `local_guidance` as appropriate. A report
  of visibility has no category yet; adding `underwater_visibility` is a domain change that needs
  risk-reviewer sign-off and is listed under open decisions.
- **Structured fields only.** Fixed-choice or bounded-number fields, each with an explicit unit:
  observed time, site id, current strength and direction (as the observer judged it), surge,
  surface conditions, visibility distance, site accessible yes/no. At most one short note, stored
  and displayed as plain text, **never placed in a prompt**, because it is untrusted input.
- **No thresholds.** The intake records what was seen. It does not classify a value as safe or
  unsafe. Any limit comes from the dive professional's signed pack, as for every other factor.
- **Quality.** Observations start at `unassessed` and never above `degraded` until the dive
  professional states how far one named observer is to be trusted. A single report cannot support
  `GO` or `CAUTION` under the current policy (ADR 0008).
- **Tighten only.** An observation of a hazard can force `NO-GO` or `INSUFFICIENT EVIDENCE` through
  a reviewed rule. An observation of good conditions can never relax a result alone.
- **Who may submit.** A new `observer` role in the API key table (ADR 0013), separate from
  assessor and decider, with its own rate limit. The observer name comes from the credential, never
  from the body.
- **Write-once.** A report is never edited. A correction is a new report that names the one it
  corrects. Records are append-only like assessments (ADR 0012, 0015): a new table, a migration,
  and a trigger that forbids update and delete.
- **Stale and future reports.** A report older than the policy window, dated in the future, or
  for a different site is rejected at intake, not stored as valid.
- **Contradiction.** Two recent reports for the same site that disagree are reported as a
  contradiction (`INSUFFICIENT EVIDENCE`), not averaged.

## Open decisions for the owner

1. Which operators may hold an `observer` key, and who issues and revokes keys.
2. Whether to add `underwater_visibility` (and perhaps `surge`) as new evidence categories.
3. Whether the dive professional will define the observer scale for current strength (a wording
   scale agreed with the operators, not a number we invent).
4. Whether a report may be entered on an observer's behalf (a named entrant and a named observer).
5. Retention and who may read observer names, since they identify people.

## Not solved

- One observer is a single point of opinion; there is no corroboration scoring.
- A key can be shared or borrowed; the name is only as real as key custody.
- No photo or file attachments (not planned: untrusted content, size and privacy).
- Reports are per process rate limited only (ADR 0013).

## Build order if accepted

1. Decisions 1 to 3 answered. 2. Domain change and risk-reviewer. 3. Migration, repository,
route, role. 4. Security-reviewer on role, input handling and storage. 5. Failure tests and
mutation checks. 6. Web form.
