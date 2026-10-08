# ADR 0004: Human override policy

- Status: Accepted
- Date: 2026-10-09

## Context

The final decision belongs to the diver, dive master or dive leader. The system is decision
support, not a safety authority, and cannot know local conditions, briefings or experience that
a qualified human has. The open question was whether a human may override a hard `NO-GO` to a
less severe outcome.

## Decision

A human may override any recommendation, including `NO-GO`, to a less severe outcome. No second
approver is required. Safeguards:

- The override needs an identified `decided_by` and a non-blank `override_rationale`.
- `is_override` must equal "human decision differs from the recommendation".
- The system's `final_recommendation`, the deterministic result and the evidence are never
  altered by an override; the human decision is recorded alongside them.
- `AssessmentRecord.overrides_to_less_severe` flags these cases for review and evaluation
  (see [evaluation-strategy.md](../evaluation-strategy.md)).

This applies to humans only. Agents, LLMs, ToT and models can never relax the deterministic
result (ADR 0001); `reconcile` is unchanged.

## Consequences

- The system remains advisory; responsibility for a less severe decision rests with the named human.
- The audit trail makes every such decision reviewable.
- The user interface should show the deterministic rule results prominently before accepting a
  less severe override, and the API must authenticate the actor (see Known gaps in
  [architecture.md](../architecture.md)). Without authentication the recorded identity is not trustworthy.

## Revisit if

Evaluation shows frequent unjustified less severe overrides, or the product is used by
operators who need a second-approver rule.
