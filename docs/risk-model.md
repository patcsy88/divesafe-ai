# Risk model

Two distinct mechanisms, ordered by authority.

## 1. Deterministic rules engine (authoritative)

`divesafe.risk.RiskRulesEngine`: pure, no I/O, no LLM.

- **Rules** implement `Rule.evaluate(plan, evidence) -> RuleResult | None` and return an outcome
  (`GO`, `CAUTION`, `NO-GO`, `INSUFFICIENT EVIDENCE`), a rationale and evidence IDs.
- **Evidence policy** (`EvidencePolicy`): required categories and maximum evidence age
  (must be positive). Missing, stale or future-dated (`retrieved_at` after now) required
  categories produce `INSUFFICIENT EVIDENCE` results automatically. All datetimes must be
  timezone-aware.
- **Aggregation:** the most severe result wins:
  `GO < CAUTION < INSUFFICIENT EVIDENCE < NO-GO`.
- **Versioning:** every ruleset has a `ruleset_version` stored in the audit record.

### Thresholds

The engine ships **no numeric thresholds**. Thresholds (wave height, current speed, wind,
visibility, temperature, depth/experience limits, and so on) must:

1. come from a cited, reviewed source (agency guidance, training-organisation standard, or
   a site's local rules), recorded next to the rule;
2. be configured per site and per diver-qualification context where relevant;
3. be reviewed by a qualified diving professional before use in any real-world setting;
4. be changed only through a reviewed, versioned change, never at runtime by an agent.

Until a reviewed ruleset exists, the system can only return `INSUFFICIENT EVIDENCE` or the
results of rules that are explicitly provided. The engine **fails closed**: `GO` requires at
least one rule that explicitly returned `GO` and nothing more severe. An empty ruleset, rules
that all return "does not apply", and a rule that raises all produce `INSUFFICIENT EVIDENCE`.
Every rule must carry a non-blank `citation`; the engine refuses rules without one.

## 2. Reconciliation

`reconcile(deterministic, proposed)` returns the more severe of the rules result and any
agent/LLM proposal. A less severe proposal is discarded and flagged as
`llm_attempted_downgrade` in the record. This is the single point that produces the final
recommendation.

## Audit record invariants

`AssessmentRecord` rejects inconsistent records at construction: unique evidence IDs; cited IDs
must exist; `deterministic_recommendation` must equal the most severe rule result (or
`INSUFFICIENT EVIDENCE` with none); `final_recommendation` must equal the more severe of rules
and proposal; the downgrade flag must match; `is_override` must equal "human decision differs
from recommendation"; overrides need a non-blank rationale and an identified decision-maker;
actual conditions require a human decision. A record without a `HumanDecision` has status
`PENDING_HUMAN` and must not be acted on.

## Non-numeric policy rules

- `WarningNeedsHumanReadingRule` ([ADR 0005](adr/0005-warnings-need-human-reading.md)): any
  marine warning or undated advisory in the dive window gives `INSUFFICIENT EVIDENCE`, because
  applicability cannot be determined from free text.

## Human override policy ([ADR 0004](adr/0004-human-override-policy.md))

A human may override any recommendation, including `NO-GO`, to a less severe outcome. The
system's recommendation is never altered; the override is a separate `HumanDecision` that needs
an identified decision-maker and a non-blank rationale. Less severe overrides are flagged
(`AssessmentRecord.overrides_to_less_severe`) for review and evaluation.

## Known gaps (tracked, not yet implemented)

- **Contradictory evidence:** the engine checks presence and freshness only. Contradiction
  detection must be defined per category in the reviewed ruleset (no tolerance is invented in
  code). Until then, same-category conflicts are not detected.
- **Validity window (must be done before any GO rule is accepted):** freshness uses
  `retrieved_at` only. Coverage of the dive window by `valid_at` is enforced inside the marine
  connector (all hours or nothing per category) but not by the engine, so other connectors and
  the `no_active_warnings` item are not protected.
- **Per-category quality policy:** category-only sufficiency lets coarse model currents
  (about 8 km, `grid_distance_km` recorded) satisfy `currents`. `EvidencePolicy` needs reviewed,
  per-category accepted data kinds and resolution. Until then model currents alone must not be
  allowed to support a `GO`.
- **Empty `required_categories`:** an engine with a `GO` rule and no required categories returns
  `GO` on no evidence. Reject or flag this when the orchestrator is built.
- **Connector `issues`** must be carried into the `AssessmentRecord` and shown to the user even
  for categories that are not required.
- **Prompt layer:** warning text is untrusted; it must be delimited and never reach a tool or
  decide an outcome. A hostile-text safety test exists for the rules path; the prompt-side test
  belongs with the agents.
- **Rule review metadata:** rules carry a citation; reviewer and review-date fields are not yet
  enforced.

## 3. Statistical risk model (advisory, later phase)

Estimates risk and confidence from historical observations and outcomes. Constraints:

- Advisory: it can add `CAUTION` or raise concern but cannot relax any rule result.
- Must report calibration and uncertainty; below a minimum data volume it abstains.
- Trained only on data with provenance; model version stored with every assessment.
- Learning from feedback never edits rules automatically; it produces proposals for review.

## Confidence

Confidence in `[0, 1]` reflects data freshness, forecast uncertainty, source agreement and
rule coverage. It describes the *assessment's* reliability, never the safety of the dive. Low
confidence must be shown to the user prominently. Confidence never reduces severity; it can
only keep or tighten an outcome.

## Test requirements

`tests/safety` must continue to prove: severity ordering; no relaxation by proposals; stale or
missing evidence gives `INSUFFICIENT EVIDENCE`; hard `NO-GO` outranks everything.
