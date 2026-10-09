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

### Risk factors and placeholders

The engine must eventually evaluate (`RiskFactorKind`): wind, wave height, swell, swell period,
current, tidal current, weather, marine warnings, forecast uncertainty, data freshness and
site-specific constraints. Today only two are evaluated, and neither needs a number:
marine warnings (`WarningNeedsHumanReadingRule`) and data freshness (the engine's evidence
policy). Every threshold-dependent factor has a **placeholder** (`ThresholdTbdRule`): its limit
is `TBD - REQUIRES DOMAIN VALIDATION`, it reports that the factor is NOT evaluated and returns
`INSUFFICIENT EVIDENCE`. `RuleResult` refuses `GO` or `CAUTION` for a `TBD` or
`REQUIRES DOMAIN VALIDATION` threshold, and `RiskAssessment.unevaluated_factors` (stored on the
record and shown by the API) lists every factor with no `VALIDATED` rule.

**Coverage guard.** `RiskRulesEngine` takes `required_factors` (default: every threshold factor).
If the most severe result is `GO` or `CAUTION` while a required factor has no `VALIDATED` rule,
the engine appends `ruleset.threshold_factors_uncovered` (`INSUFFICIENT EVIDENCE`). So `GO` or
`CAUTION` is only reachable once a validated rule covers all required factors, even if the
placeholders are left out of the ruleset. `RuleResult` also refuses `GO` or `CAUTION` for a
threshold factor unless its threshold is `VALIDATED`, whether or not `threshold_status` was
given. Passing `required_factors=frozenset()` disables the guard and is for synthetic tests only.

Residual limits: a rule can still claim `VALIDATED` falsely, so reviewer sign-off on every rule
that can produce `GO` or `CAUTION` is a process control, not a code guarantee. A numeric literal
test scans every module under `risk/` (any float, any int outside the severity ranks 0-3,
numeric-looking strings; it has a negative test proving it trips) but cannot see a limit loaded
from a file or another package, so threshold sources must be named, versioned artefacts. The
physical-plausibility bounds in `domain/conditions.py` (non-negative, 0-360) are not safety
thresholds; no limit may be added there.

### Thresholds

The pack a dive professional completes to supply them is in
[domain-validation/](domain-validation/README.md): five decisions and per-factor forms with every
limit blank, the data we actually hold for each factor, and a sign-off checklist.

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

## Pipeline guarantees (`divesafe.orchestration`)

- `assess_dive` refuses a policy with no required categories (`UnsafeConfigurationError`), so a
  `GO` rule cannot fire with an empty required-evidence policy through the pipeline. (It does not
  prove a rule reads the categories the policy requires.)
- Failed or rejected sources never abort the assessment. They are recorded in
  `AssessmentRecord.evidence_issues` and add an `evidence.connector_issues` rule result that
  forces at least `INSUFFICIENT EVIDENCE`, even for categories that are not required.
- `confidence` is `None` (not computed) until a risk model exists; it is never defaulted.
- Every result is `PENDING_HUMAN`. `decide()` derives `is_override` from the decision, requires a
  rationale for overrides, refuses to replace a recorded decision, and returns a new revalidated
  record. `report_actual_conditions()` requires a decision and can be used once.

- The record itself rejects: evidence issues with a deterministic result below `INSUFFICIENT
  EVIDENCE`; a decision dated before the assessment; actual conditions dated before the decision.
  The pipeline rejects a `planned_start` in the past.

### Pipeline limits (known gaps)

- **Authentication (interim):** the API takes the actor from an API key (or, in development
  only, a header) and never from the request body ([ADR 0006](adr/0006-api-auth-and-repository.md)).
  No roles: any authenticated actor may decide or override. `decide()` itself trusts its
  caller, so it must only be reachable through the API.
- **Write-once:** `check_successor` plus compare-and-set `replace` enforce it in the repository
  contract (in-memory adapter tested, including concurrent writers). A durable adapter must meet
  the same contract; unvalidated `model_copy` records are untrusted.
- **Decision-time staleness:** `DIVESAFE_DECISION_MAX_AGE_MINUTES` rejects decisions on old
  assessments, but there is no default (unset means no limit) and evidence is not re-checked.
- **`now`** is the server clock in the API. `assess_dive` still takes it as a parameter, so
  non-API callers must pass a trusted clock.
- **Untrusted inputs:** `proposed`, `scenarios` and `explanation` must come only from the agent
  layer, which must delimit untrusted text. They are not sanitised here.
- **Durability:** the only repository is in-memory, so a restart loses every assessment and
  decision, and several workers would each hold different data. Run a single worker; the API
  reports `storage` on `/health`, warns at startup and refuses to start in production.
  Actual-conditions reporting has no role restriction.
- **`confidence=None` means "not computed".** UIs must say so explicitly and never render it as
  0 or as high. The statement that a `GO` rule cannot fire on no evidence only covers an empty
  required set; unrequired-but-absent data, contradictory data and window coverage remain open
  (see above).

### Agent output (ADR 0007)

- A proposal can only tighten the result; a less severe one is discarded and flagged. A failed,
  skipped or rejected agent stage leaves the deterministic result standing, with the reasons in
  `agent_issues`.
- `explanation`, `findings` and `scenarios` are LLM-generated and unverified. The API labels
  them. Specialist and scenario `confidence` values are uncalibrated self-estimates and are not
  the record `confidence`, which stays `None`.
- Known gap: only a scripted fake LLM has been used, so injection resistance and output
  reliability of real models are unmeasured. An evaluation set (tests/evaluation) is needed
  before any real provider is trusted for even advisory output.

### Evidence usability (ADR 0008)

A required category is satisfied only by evidence that is fresh, of an accepted *effective*
quality (`domain.effective_quality` caps a connector's claim: only a measured observation with
unit and range checks and no limitations can be VALIDATED) and that covers the planned dive
window (intervals must chain without a gap; instants are bracketed; notices and knowledge are
exempt only for their own categories and only if they have not ended before the dive).
An observation cannot be valid after it was retrieved. Degraded evidence can make a category
sufficient but cannot support `GO` or `CAUTION` unless `degraded_may_support_go=True`, which the
default and the production configuration do not set, and validated evidence must itself cover the
window. A `GO` or `CAUTION` that cites stale or unaccepted evidence is downgraded. Rules still
receive all evidence so hazard rules are never blinded.

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
- **Validity window and quality are now enforced** by the engine ([ADR 0008](adr/0008-evidence-usability.md)).
  What remains before any `GO` rule is accepted:
  - *Per-variable and per-location checks:* sufficiency is per category, so a covering item with a
    different variable (wave height without swell period) or another grid cell still satisfies the
    category. `grid_distance_km` is recorded but not limited. A reviewed required-variable and
    location rule is needed.
  - *A `GO` that cites nothing* passes the citation guard (an empty citation is a subset of any
    pool). Real rules must cite evidence; consider enforcing it when the first real rule lands.
  - *Per-category resolution and kind policy:* model currents at about 8 km are accepted as
    DEGRADED evidence; whether that is good enough for `GO` is the owner decision
    `degraded_may_support_go` (default `False`).
- **Prompt layer:** warning text is untrusted; it must be delimited and never reach a tool or
  decide an outcome. A hostile-text safety test exists for the rules path; the prompt-side test
  belongs with the agents.
- **Rule scope, expiry and per-factor age (needed before any limit is encoded):** `Rule.evaluate`
  receives only a `DivePlan` (site, start, duration, maximum depth). There is no diver
  qualification, dive type, season, exposure direction, forecast margin or per-factor data age,
  so a limit scoped to those cannot be represented, and a rule has no review expiry, so a stale
  `VALIDATED` rule would keep permitting `GO`. The domain-validation pack collects all of these;
  the software must gain a rule-definition model that carries scope, margin, source, reviewer and
  an expiry that fails closed to `INSUFFICIENT EVIDENCE`, and a limit whose scope it cannot
  represent must be refused, not widened. A second independent reviewer for any rule that can
  allow `GO` or `CAUTION` is a process control, not a code guarantee.
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

## Known gaps (domain and risk foundation)

- **Evidence usability** is checked ([ADR 0008](adr/0008-evidence-usability.md)). Residual limits:
  gaps inside a series of instants are not detected (a maximum gap would be an invented number);
  nothing can earn VALIDATED for a future window (an observation cannot describe the future), so
  `GO` needs the owner decision `degraded_may_support_go`, default `False`; a connector could
  falsely add check steps, which is dormant while none claims VALIDATED; the quality caps and the
  (kind, category) exemption list are policy that needs review.
- **`ConfidenceAssessment`** documents the intended shape but is not wired into the record. The
  record's own `confidence` is enforced instead: a number requires `confidence_method`.
- **Rule exceptions and invalid citations:** a rule that raises becomes `INSUFFICIENT EVIDENCE`,
  but a rule citing evidence ids that are not in the record makes record construction fail, which
  the API reports as an opaque 500 rather than a visible `INSUFFICIENT EVIDENCE` assessment.
