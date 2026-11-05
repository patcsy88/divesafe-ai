# ADR 0026: Most severe variant for conflicting evidence, and decision roles

- Status: ACCEPTED (owner sign-off 2026-11-05, including the reassignment trigger migration).
  The capacity numbers are chosen by the owner at build time. Each part needs review by
  `risk-reviewer` and `security-reviewer` before it is done.
- Amends ADR 0004 (override policy), ADR 0008 (evidence conflicts, "Deferred" option), ADR 0013
  (roles) and ADR 0014 (decider assignment). Builds on ADR 0012 (old-row rule) and ADR 0015
  (triggers are never disabled).

## Context

Two open gaps, both listed in `docs/architecture.md` Known gaps:

1. When two different evidence items share one id, `build_evidence_set` drops all of them and
   records `evidence.conflict`. The result is INSUFFICIENT EVIDENCE, but a dropped item may have
   crossed a signed-off limit or been a marine warning, so NO-GO becomes INSUFFICIENT EVIDENCE.
   The owner deferred "evaluate each variant and keep the most severe outcome" on 2026-10-25, and
   it must be decided before any validated rule ships. On 2026-11-04 the owner chose to build it.
2. Any `decider` may override to a less severe outcome; one person can hold assessor and decider;
   reads are not scoped; a decider cannot be replaced; the decision does not say which key made it.

## Part A: keep the most severe variant

**Who decides: the deterministic engine, not an agent.** Tree-of-Thought (breadth- or depth-first)
is not used. The answer must be the maximum over all combinations, so every combination has to be
evaluated; BFS and DFS visit the same set, and the only saving is stopping early once a combination
returns NO-GO (the maximum). A model-driven search could miss the dangerous variant, and
AGENTS.md ranks deterministic rules above agent reasoning. The engine is pure, so each run is
reproducible.

**Algorithm (code only, in `services/evidence.py` and `orchestration/pipeline.py`):**

1. `build_evidence_set` keeps, for each conflicting id, the distinct variants in arrival order
   (it already groups them). The main evidence set still excludes all of them, as today.
2. A combination takes exactly one variant for every conflicting id and adds those items to the
   main set. The pipeline runs the engine on each combination at the same clock.
3. Order: combinations are tried in a fixed order (sorted by variant index). The search stops early
   when a combination returns NO-GO. The result does not depend on the order, only the time does.
4. Each combination whose outcome is more severe than the main run adds a rule result
   `evidence.conflict_variant` (outcome = that combination's outcome, rationale a fixed sentence
   naming the category and connectors, never item content). `reconcile` then takes the most
   severe of all rule results as it does today, so nothing can relax.
5. The audit record keeps the withheld items, adds which combination was worst (variant indexes by
   connector name), and the page shows it next to the withheld evidence.
6. Variant items are never cited, never passed to agents or prompts, and never count toward
   "required categories satisfied": they can only add severity. A combination that returns GO or
   CAUTION adds nothing.

**Capacity bounds (not safety limits):** at most `MAX_CONFLICT_IDS` conflicting ids and
`MAX_VARIANTS_PER_ID` variants each, and at most `MAX_COMBINATIONS` runs. Beyond a bound the
engine does not guess: it adds `evidence.conflict_unbounded` (INSUFFICIENT EVIDENCE) and the main
result stands. The numbers are chosen in the build and recorded here; they protect run time only.

**Consequences:** a hostile or faulty source can force NO-GO by sending an invented dangerous
variant. That is the safe direction, it is visible to the human, and a human can still override
(Part B decides who). When this is built, the start-up guard `DIVESAFE_ACCEPT_EVIDENCE_CONFLICT_GAP`
and its docs are removed, and the Known gaps entry in `architecture.md` and ADR 0008 is closed.

**Tests:** a conflict where one variant would cross a limit gives NO-GO, not INSUFFICIENT
EVIDENCE; order independence; early stop gives the same result; every bound; no variant item
reaches a prompt; mutation checks (drop the combination loop, keep the first variant only, let a
variant relax). A rule set with a validated definition is needed to exercise a limit; a test
definition with a labelled synthetic limit is used, never a real one.

## Part B: roles and authorization

**1. `overrider` role.** A `decider` may accept the recommendation or choose a MORE severe
outcome. Choosing a LESS severe outcome than `final_recommendation` needs `overrider` as well. Roles
do not imply each other (ADR 0013); an `overrider` key must also be a `decider` and a `viewer`,
refused at start-up otherwise. A decider without `overrider` who chooses a less severe outcome gets
403, and nothing is recorded (the assessment stays pending, as with every 403). A rationale and
identity are still required (ADR 0004). This amends ADR 0004: "a human may override" becomes "a
human with the `overrider` role may override to a less severe outcome". No second approver (ADR
0004 stays on that).

**2. Self-approval stays allowed and is made visible.** `HumanDecision` gains
`decided_by_creator: bool` (set by code: `decided_by` equals `created_by`, case-insensitive). The
page and the evaluation output show it. It never blocks a decision.

**3. Which key decided.** `HumanDecision` gains `key_id`: the first 12 hex characters of the key's
sha256 hash (a non-secret label; the hash is already the stored form). Dev authentication records
`dev`. Closes ADR 0006 production gate 4.

**4. Reassignment (`admin` role).** An `admin` may move an unfinished (still pending) assessment to
another `decider` through a new endpoint. Rules: roles do not imply each other; `admin` does not
let someone decide; the new assignee must be a known key holder with `decider` (ADR 0014); a
decided assessment cannot be reassigned; each reassignment is appended to `reassignments` (who,
when, from, to) and never edited. The first assignee loses the right to decide immediately. The
endpoint is rate limited like `decide`.

**5. Read scoping.** Key config gains an optional `groups` list. A `viewer` sees assessments whose
`created_by` or `assigned_decider` shares a group with them, or that they created or are assigned
to. A key with no groups keeps today's behaviour (reads everything) so existing deployments do not
change until groups are configured; production start-up logs a warning while any viewer key has
no groups. An unreadable assessment returns 404, the same as a missing one, so ids cannot be probed.

**Config format:** `{"actor": ..., "roles": [...], "groups": [...]}`; `groups` is optional.

## Storage (the hard part)

- `human_decision` is write-once and the trigger refuses any change to it once set (ADR 0015).
  The new `HumanDecision` fields (`decided_by_creator`, `key_id`) are nested defaulted fields. An old
  pending row has no decision yet, so it is fine. But an old DECIDED row, when actual conditions
  are later added, would have the new keys written into its `human_decision` and be refused. So
  `_NESTED_NEW_DEFAULTS` (ADR 0012) must cover `human_decision` too, with a real-database test
  that decides an old-shape row, then reports actual conditions.
- `reassignments` and the withheld-variant fields are new top-level defaulted fields. The trigger
  requires every key except `human_decision` and `actual_conditions` to stay identical, so
  reassigning an existing pending row **needs a trigger change**: allow `reassignments` (and
  `assigned_decider`) to change while `human_decision` is null, append-only, in a new migration
  (ADR 0015 allows migrations; the triggers are never disabled). **This is the one change that
  alters the storage guard; the owner must approve it explicitly.**
- The in-memory repository gets the same rules and `tests/unit/test_repository_contract.py` is
  extended for both adapters.

## What this does not change

`reconcile`, the deterministic result, the proposal and the final recommendation stay as they are.
Authorization decides who may record a decision; it never changes what the system recommends.
Part A can only add severity.

## Review and build order

1. Sign-off on this ADR, including the trigger change.
2. Part A (evidence), reviewed by `risk-reviewer` and `security-reviewer`.
3. Part B (roles, key id, self-approval flag, then reassignment and read scoping), same reviewers.
   The `overrider` split and the key id come first; reassignment needs the migration.
4. Docs: close the ADR 0008, 0013, 0014 and `architecture.md` Known gaps that this fixes, and
   record the rest.

## Known gaps (planned)

- Variants are compared only within one id; a conflict across different ids is not a conflict.
- Group names are free text in the key config; there is no directory and no check that a group is
  meaningful.
- Key rotation, expiry and revocation stay manual. A removed decider's assessments still need an
  `admin` to reassign them.
- Roles still do not say who is qualified to take a dive decision.
- Rate limits stay per process (ADR 0013).
