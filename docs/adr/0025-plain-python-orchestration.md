# ADR 0025: Plain-Python orchestration with an explicit stage graph

- Status: PROPOSED (2026-10-26). Needs `risk-reviewer` and `security-reviewer` (it touches
  `orchestration`, `agents`, `domain` and `tests/safety`). Owner decision recorded after a review
  of a CrewAI proposal. Items 2 to 6 are the build order; item 2 is built (see its note).
- Builds on ADR 0001 (hierarchy), 0007 (agent stage), 0011 (line 28: "LangGraph is not adopted"),
  0012 (old-row rule), 0016/0018 (adapters, evaluation), 0022 (RAG), 0023 (egress), 0024 (compose
  pins).

## Context

`assess_dive` (`orchestration/pipeline.py`) is one function: gather evidence, run the engine, run
the agent stage (`agents/stage.py`: five specialists, three scenarios, one proposal, at most 9
calls, one stage deadline), re-judge the rules at the later time, `reconcile`, build the record.
The lifecycle in AGENTS.md section 2 is implied by the code's order, not represented in it, so a
test cannot assert "no path reaches GO without the engine" except by reading the function. The
record keeps no per-stage trace. Evaluation (`tests/evaluation/harness.py`) runs the whole stage
or nothing. A proposal to adopt CrewAI was reviewed and declined by the owner; this ADR records
why and what is built instead. Project scope stays Malaysia-only; outcomes stay `GO`, `CAUTION`,
`INSUFFICIENT EVIDENCE`, `NO-GO` in that severity order. Nothing here claims the system guarantees
safety.

## Options considered

Claims about third-party libraries are as documented by the vendor at the time of writing; not
verified here. Nothing was fetched or run.

| | Dependency surface | Keeps `LLMProvider`, egress, delimiting | Retry-on-validation vs refuse-never-repair (AGENTS 12) | Telemetry / egress risk | Fit with ADR 0023/0024 | Cost to re-prove ~2,900 safety tests |
| --- | --- | --- | --- | --- | --- | --- |
| CrewAI (Crews, Flows) | Large transitive set (its own model client, tooling, memory, storage) | No: calls models through its own client unless a custom LLM class wraps ours; prompts are composed by the framework, so the `<untrusted_data>` layer (ADR 0007) is not the only entry | As documented, would violate: task guardrails re-run the task with the error fed back to the model (bounded retries), the repair loop section 12 forbids | Vendor telemetry reported as on by default with an opt-out; must be audited and pinned off | Poor: ADR 0023's text scan for adapter constructors and ADR 0024's exact settings pins would both need widening | Full: every prompt, call-count, timeout and citation test is re-proved against framework code |
| LangGraph | Moderate (`langgraph`, `langchain-core`); checkpointers optional | Partly: nodes are plain functions, so `LLMProvider` can stay, but the state graph, checkpoints and retry policies are the framework's | Retry policies are per node and off by default; the repair loop is avoidable, not prevented | Tracing is opt-in by environment variable; a stray variable turns it on | Moderate: no model egress of its own; adds code paths the compose pins do not cover | Partial: stage and pipeline tests re-proved; agent-layer tests kept |
| Pydantic AI | Moderate (Pydantic already present; model clients per vendor) | No: its agents own the model call; a custom model class would be needed | As documented, would violate by default: output validation failures are sent back to the model for a bounded number of retries | Instrumentation is opt-in | Poor to moderate: same adapter-scan and pin problems as CrewAI | Full for the agent layer |
| Workflow engine (Temporal and similar) | Heavy: a server, a database, an SDK | Yes for prompts (activities are our code), but adds a durable-state service to the deployment | Activity retries are the point of the engine; must be disabled per activity | Server-side history stores activity inputs (prompts, evidence) unless excluded | Poor: a new service, new secrets, new pins in ADR 0024 | Partial, plus a new ADR for the service |
| **Plain Python, explicit graph** (chosen) | None | Yes, unchanged | Enforced by our code, as today | None added | Unchanged | Low: behaviour-neutral refactor; existing tests stay as the oracle |

No framework is needed for a capability the project lacks (ADR 0011's test still holds: no
checkpointing, no long jobs beyond ADR 0019's inline runner, no fan-out past 14 calls). Each
framework either replaces the one model path the safety tests pin or brings a retry-with-error
loop whose default must be switched off and then proved off. The chosen option makes the graph
visible to tests without changing who may write what.

## Decision

Plain Python with the lifecycle as data. Any later framework adoption needs a new ADR read by
both reviewers before code. Build order, each item with failure tests, mutation checks and both
reviews before the next:

**1. This ADR.**

**2. A1 explicit stage graph and A4 Supervisor (behaviour-neutral refactor of `assess_dive`).**

- `orchestration/graph.py` holds `STAGES: tuple[Stage, ...]`; a `Stage` is a frozen dataclass
  with `name`, `requires` (input names), `produces` (output names), a `route` function (the next
  stage, given the outcome) and an optional `timeout_seconds`. Order, matching today's data flow
  (agents receive the engine's rule results, `pipeline.py` lines 92-110): `Observe` (connectors)
  -> `Retrieve` (placeholder until RAG stage 1, ADR 0022; outcome `not_configured`) -> `Assess`
  (engine) -> `Reason` (`run_agent_stage`, unchanged) -> `Rejudge` (engine again at the later
  clock, `WINDOW_STARTED_RULE`, as today) -> `Propose` (`reconcile`) -> `Supervise` (A4) -> `Gate`
  (the record is `PENDING_HUMAN`; nothing runs after it).
- Graph-property tests run on the data, not on a run: no path reaches `Gate` without `Assess`,
  `Rejudge`, `Propose` and `Supervise`; `Reason` requires `Assess`'s output; `Supervise` always
  follows `Propose`; `Propose` is the only stage producing `final_recommendation`; every
  `requires` is produced by an earlier stage. `Assess`, `Rejudge`, `Propose` and `Supervise` may
  only end `completed`; any other outcome means no record (the run fails as today).
- **Supervise is code, not an LLM.** It writes a `SupervisorVerdict` (`passed`, `failed`: tuples
  of fixed check names; `tightened_to: Recommendation | None`; no free text). Checks:
  `reconcile_tighten_only` (final is at least both inputs and `llm_attempted_downgrade` agrees;
  a mislabelled flag is caught here too),
  `severity_monotonic` (final at least deterministic), `citations_valid` (proposal, finding and
  scenario ids are in the evidence list), `prompt_version_pinned` (`model_version` carries
  `prompts=<PROMPT_VERSION>` whenever a provider ran), `required_categories_present` (a missing
  required category implies at least `INSUFFICIENT EVIDENCE`; presence-only, the engine still
  judges usability).
- **Two classes of failure.** `reconcile_tighten_only` or `severity_monotonic` failing is an
  invariant breach: raise, store nothing. The other three tighten: Supervise appends a rule result
  `supervisor.<check>` with outcome `INSUFFICIENT EVIDENCE` (the mechanism already used for
  `evidence.connector_issues`) and `reconcile` is called again with the unchanged proposal, so
  `final` is still produced only by `reconcile` (AGENTS 5) and `tightened_to` is the new final.
  After a tightening the downgrade-explanation step is rerun and the existing notice "agent
  output reflects the earlier result" is added. A property test over all severity pairs proves
  Supervise never makes `final` less severe than Propose's. A new `AssessmentRecord` validator
  requires a `supervisor.<check>` rule result for every failed check and only then. Because
  `deterministic_recommendation` then includes Supervise's results, the "Rules alone:" label in
  `api/schemas.py` (about line 295) gets new wording at item 2.
- Behaviour-neutral means: on current code every check passes on every reachable path, because
  the upstream check (runner id check, `reconcile`, the engine's policy) already holds. Teeth are
  proved by mutating the upstream check and watching Supervise fail the case. The verdict is a
  new top-level field `supervisor_verdict: SupervisorVerdict | None = None` (ADR 0012 replace
  rule; no nested field).
- **Built (2026-10-26, branch `rag-stage-1`; tests by the test-engineer).** `orchestration/graph.py`
  (`Stage`, `STAGES`, `next_stage`, `walk`), `domain/stages.py` (`StageName`, `StageOutcome`,
  in `domain` because item 3 stores them), `domain.SupervisorCheck`, `domain.SupervisorVerdict`,
  `AssessmentRecord.supervisor_verdict`, `orchestration/supervisor.py` (`supervise`,
  `SupervisorInvariantError`), `assess_dive` walking `STAGES` through `next_stage`. Deviations
  and details: `Retrieve` has an empty `produces` (nothing flows from it yet); `Reason` ends
  `skipped` with no provider and `timed_out` when the agent stage reports its deadline, and
  every outcome routes to `Rejudge`; `Supervise`'s `requires` lists `final_recommendation`,
  `rule_results`, `deterministic`, `proposed`, `evidence`; `Gate` produces `record`. The
  `SupervisorVerdict` validator itself refuses an invariant check in `failed`, a duplicated
  check and a `tightened_to` with no failed check; the record validator also requires
  `tightened_to` to equal `final_recommendation` when set. `prompt_version_pinned` is checked
  only when a provider ran (owner decision); `citations_valid` covers proposal, finding and
  scenario ids against the evaluated (not withheld) evidence ids;
  `required_categories_present` is presence-only: it compares the policy's required categories
  with the categories that have any evidence item (a stale, rejected-quality or out-of-window
  item still counts as present; the engine's usability judgement remains authoritative) and
  passes when the deterministic result is already at least INSUFFICIENT EVIDENCE.
  `reconcile_tighten_only` also catches a mislabelled `llm_attempted_downgrade`. A failed
  tightening check is shown in the API outcome note as fixed text with the check name.
  `supervisor.` is a reserved rule prefix (`risk.engine.RESERVED_RULE_PREFIXES`). The tightening
  notice is its own fixed text ("the supervisor tightened the result after the agent stage;
  agent output reflects the earlier result"), added once. The "Rules alone:" label became "Deterministic checks:". No migration:
  `supervisor_verdict` is a top-level defaulted key, which `replace()` omits for old rows
  (ADR 0012). The field is stored but not exposed on the API view (owner decision). The one
  existing test that pins the old label (`test_llm_forced_severity_is_attributed_not_presented_as_a_rule_result`)
  is the test-engineer's to update.

**3. A2 stage trace on the record.** `AssessmentRecord.stages: tuple[StageTrace, ...] = ()`, a
new top-level field with a default, which ADR 0012's `replace()` rule permits for old rows.
**No nested field is added**: `Finding`, `Scenario`, `RuleResult` and `EvidenceItem` are
unchanged. `StageTrace` holds `name` and `next_stage` (from the fixed stage-name list),
`started_at`, `finished_at`, `evidence_ids`, `outcome` (fixed enum: `completed`, `skipped`,
`timed_out`, `failed`, `not_configured`) and `issues`. Rules:
- `issues` are codes from one fixed enumerated list, each formed from stage name, connector or
  task name, error class and outcome (for example `reason.specialist:weather.timed_out`), with a
  per-code length cap, no duplicates, never copied from `evidence_issues` or `agent_issues` (those
  hold clipped connector text and provider error class names and are stored already). When more
  than the bound would be recorded, one fixed `overflow` code is written. The trace never trims
  `evidence_issues` or `agent_issues` and never aborts the assessment.
- No prompts, no LLM text, no provider error text. Stage logs carry only name, outcome, duration
  and counts.
- `evidence_ids` enter the record's cited-id check (`models.py` about lines 350-358) and never
  include withheld ids.
- Bounds: at most `len(STAGES)` entries, one per stage, in graph order; at most
  `MAX_EVIDENCE_ITEMS` (120, the existing constant) ids per stage; the per-stage issue bound is
  set from the size of the fixed code list (for example the number of distinct codes a stage can
  emit), decided at item 3 with the reviewers, not a safety threshold.
- The API view and the web timeline are deferred; the stored field comes first.
- **Built (2026-10-26, branch `rag-stage-1`; tests by the test-engineer).** `domain/stages.py`
  holds `StageTrace`, `STAGE_ORDER` (the graph refuses to load if `STAGES` disagrees),
  `ISSUE_CODE_PATTERN` (`^[a-z]+\.[a-z_]+(:[a-z0-9_\-]{1,40})?$`, 80 characters), `issue_code`,
  `bound_issues`, `TRACE_ISSUE_KINDS` and the bounds; `AssessmentRecord.stages` is the last,
  defaulted, top-level field (no migration: `_without_new_defaults` omits it for old rows). The
  record checks one trace per stage, `STAGE_ORDER` order, the `next_stage` chain (`None` only on
  the last) and every trace id against the kept evidence (so withheld ids are refused). Final
  code list, `<stage>.<kind>[:<qualifier>]`: observe `connector_failed:<connector>`,
  `connector_timed_out:<connector>`, `connector_issue:<connector>`, `evidence_conflict`,
  `no_evidence`; retrieve `not_configured`; assess `connector_issues_rule`,
  `unevaluated_factors`; reason `skipped_no_provider`, `timed_out`,
  `specialist_failed:<specialist>`, `scenario_failed:<kind>`, `proposal_failed`,
  `task_skipped:<task>` (a specialist, scenario kind or `risk_assessment` that did not run and
  did not fail: no evidence for the call, or an item 4 ablation switch; it replaced
  `proposal_skipped`, and a skipped task is never traced as a failure); rejudge `window_started`, `result_changed`, `rules_added` (Rejudge
  appended a rule result, even if the most severe outcome stayed); propose
  `llm_attempted_downgrade`; supervise `tightened:<check>`, `llm_attempted_downgrade` (the
  second `reconcile`, after a tightening, discarded the proposal when the first had not); any
  stage `overflow` (also written when a stage's evidence ids exceed `MAX_EVIDENCE_ITEMS`).
  Qualifiers are code-registered names only, checked with `fullmatch`; a name that fails the
  qualifier pattern is written as `unnamed_<n>`, n the 1-based position among that stage's
  non-matching names (capped at `MAX_CONNECTORS`), so distinct bad names stay distinct
  (`domain.unnamed_qualifier`; a test asserts every registered connector name matches the
  pattern). The record cross-checks the trace against itself: the observe trace's ids equal the
  kept evidence ids; `supervise.tightened:<check>` codes equal `supervisor_verdict.failed`; a
  downgrade code exists exactly when `llm_attempted_downgrade` is set (each only when that
  stage's trace exists; fixed messages, fail closed). The codes come from
  structured sources added for this: `EvidenceSet.connector_outcomes`
  (`ConnectorOutcome(name, ok | issues | failed | timed_out)`, the timeout label from the error
  class prefix `gather_evidence` itself sets) and `EvidenceSet.conflicts`;
  `AgentStageResult.task_outcomes` (`TaskOutcome(task, ok | failed | timed_out | skipped)` per
  specialist, scenario kind and `risk_assessment`, from `evaluate_scenarios_with_outcomes`);
  rule ids, the reconciliation flag and the verdict. Nothing reads `evidence_issues` or
  `agent_issues`. Bound: `MAX_STAGE_ISSUES = 3 * MAX_CONNECTORS + 2 = 26` with
  `MAX_CONNECTORS = 8` (six connectors registrable today, rounded up to a power of two; a
  capacity bound, not a safety threshold). Observe is the widest stage (three codes per
  connector plus two); per-stage maxima from the enumerated list: observe 26, retrieve 1,
  assess 2, reason 20 (2 outcome codes + 5 specialists + 3 kinds + 1 proposal code + 9
  `task_skipped` qualifiers), rejudge 3,
  propose 1, supervise 6, gate 0, so `overflow` is reachable only through a future change. Dedup
  precedes the count; beyond the bound the first 25 are kept and `overflow` is last.
  `MAX_EVIDENCE_ITEMS` (120) moved to `domain/stages.py`; `agents.runner` imports it. Timing:
  `started_at`/`finished_at` are coarse bounds, not per-stage measurements: the pipeline reads
  its clock once, in Rejudge (`run.completed`), so stages up to Rejudge are stamped `now` and
  the later stages that later time; with no clock every entry has
  `started_at == finished_at == now`. The trace does not read the clock itself (a test's
  single-shot clock would be consumed). `StageTrace.duration_ms` (default 0) is the stage's own
  wall time from `time.monotonic`, and the log line carries the same value. "Never aborts" holds
  by construction: no reachable input makes `StageTrace` raise (codes come from `issue_code`
  over fixed labels, ids from the kept evidence, bounds applied before construction); a bug
  there would be a fail-closed 500 with nothing stored, never a weaker record. On the waves-only
  scripted fixture the `weather` specialist reports `no_evidence` (a valid finding), so it never
  registers `specialist_failed` there; the specialist codes are exercised by failing a specialist
  that has evidence. `evidence_ids`: observe = kept ids; assess and rejudge =
  rule-result ids present in the evidence; reason = finding, scenario and proposal ids present
  in the evidence; others empty. The gate's own trace is taken before the record is built and
  ends the chain with `next_stage=None`. A stopped run stores nothing. Each stage logs
  `stage finished` with name, outcome, `duration_ms` and the issue count only.
  `AssessmentRecordView` inherits `stages` (serialised as plain strings and timestamps; no
  dedicated view, per the owner decision below); the web typecheck is unaffected.

**4. C5 adversarial set, C2 ablation harness, E2 authority map.** In `tests/evaluation`:
- Adversarial false-GO temptations, each with a scripted `FakeProvider` and a labelled synthetic
  fixture: stale-but-plausible evidence; a cheerful marine notice ("conditions excellent") whose
  presence still triggers `WarningNeedsHumanReadingRule`; injected instructions inside a notice
  (extending the two injection cases in `SCENARIOS`); a conflicting forecast pair for one
  category (labelled synthetic); a model proposing `GO` below the rules. Expected: false-GO 0 in
  every variant.
- Ablation is built instead of the three-variant A/B/C comparison (none exists; nothing is
  deleted). The same pipeline runs with stages switched off by keyword-only flags on
  `run_agent_stage`, default all on (`specialists`, `scenarios`, `proposal`; `cross_examination`
  once item 5 exists). The flags are **not** passed through `assess_dive`, not in `Settings` and
  not in the API; `cross_examination` is the only stage switch `Settings` may set. A safety test
  checks the pipeline's call passes no ablation flag, plus a text-scan test in the style of ADR
  0023 decision 8. With a task switched off, the Reason stage is still traced as completed; each skipped task shows as the fixed code `reason.task_skipped:<qualifier>` (the task name) and records a fixed agent issue. Only a run with no provider traces Reason itself as `skipped` (`reason.skipped_no_provider`).
  Per variant the report gives false-GO (must be 0; gated offline), abstention correctness,
  citation validity, provider call count (against the fixed cap), latency and run-to-run variance
  over repeated offline runs. Live model numbers are reported, never gated; the offline harness
  tests gate in CI.
- `docs/authority-map.md`: one table, component / may write / may never write / proving test id,
  linked from architecture.md. Documentation of what tests enforce, not a new rule.

**5. B1 cross-examination round.** After the five specialists, one bounded second call per
specialist that sees the other specialists' findings and may only **add** findings or raise
questions; it never deletes, softens or changes its own first-round finding (round one is kept as
is; round two is validated independently). Questions go in the existing `conflicting_signals`
tuple of `FindingOutput` (max 10 `ShortText`); no new output field. Behind
`DIVESAFE_AGENT_CROSS_EXAMINATION` (default false; turning it on in production needs the ADR 0024
settings pin changed and an owner decision). When on: the call cap rises from 9 to a fixed 14;
the stage timeout is derived as `stage_timeout + call_timeout` (one more parallel wave), capped
at or below the settings ceiling (`llm_stage_timeout_seconds` le=1800), and flows into the
run-deadline checks (`settings.py` lines 103 and 166-182) and the run lease; a timeout only means
no second-round finding. The flag's state is recorded as a fixed `cross_examination=on` token in
`model_version` and in the trace.
- Inputs: each peer finding goes inside the `<untrusted_data>` block with code-set `source_agent`
  and `llm_generated: true` markers; self-estimates are dropped through `agent_view`; a total
  size cap is applied before neutralising and an over-cap input skips the call (never trims);
  each call may cite only ids in the evidence list (its own categories' plus ids the peers cited);
  exactly one round, never recursive.
- Second-round findings feed the proposal only, not the scenarios, so ToT's input is unchanged
  (ADR 0007 bounds stay as they are). Stored in a new top-level tuple
  `cross_examination_findings: tuple[Finding, ...] = ()` (no field added to `Finding`, ADR 0012),
  included in the record's cited-id check.
- Safety tests: the round cannot lower `final` below the deterministic result (hostile text passed
  between specialists may still steer the proposal, which `reconcile` bounds); cannot add evidence
  ids outside the evidence list; over-long replies are refused, never trimmed or repaired; the
  fixed cap holds with the flag on and off; flag off makes exactly today's calls.

**6. C3 recorded-trace replay.** One difficult live run (a missing required source plus
conflicting forecasts) is recorded as labelled fixtures: provider replies and connector payloads,
nothing else from the raw run. Replayed offline in CI and in the demo through `FakeProvider`; its
stage trace (item 3) is what the demo shows. Raw live output stays outside the repository
(evaluation-strategy.md rule). Scrub list, backed by a test that scans `tests/fixtures`: model
host name, public and LAN IP addresses, loopback proxy port and proxy software name, bearer
tokens, keys and the MET token, request URLs and headers, provider response metadata (`id`,
`system_fingerprint`, the actual `model`, `created`), egress-basis output files, `/Users/...`
paths. PHN payloads are excluded (terms not on file, ADR 0021). Any synthetic conflicting
forecast is labelled synthetic.

## Consequences

- The lifecycle becomes assertable data; `assess_dive` keeps its signature and results.
- Three new top-level record fields with defaults (items 2, 3, 5); none nested; old rows stay
  decidable under ADR 0012's rule. Each is pinned by a test that it is top-level and defaulted.
- Supervise adds a second, recorded line of defence that duplicates checks already enforced; the
  duplication is deliberate and cheap.
- Cross-examination raises cost and latency (14 calls) only when switched on; production keeps
  the flag's default until the ablation report says the round earns its calls.
- No new dependency, no change to what leaves the machine (ADR 0023/0024 unchanged).
- CrewAI, LangGraph, Pydantic AI and workflow engines stay out; ADR 0011 line 28 now points here.

## Owner decisions (2026-10-26)

- Supervise tightens by appending a `supervisor.<check>` rule result and calling `reconcile`
  again; `reconcile`'s signature is unchanged.
- `supervisor_verdict` and `stages` are stored in items 2 and 3 but get no dedicated API view
  until the stage-trace view and the web timeline step (both fields are inherited by the record
  view as stored).
- `prompt_version_pinned` reads the `model_version` string until the typed `egress_basis` and
  `model_host` record fields (ADR 0022/0023) exist; then it moves to those fields.
- The item 6 live run uses the same declared egress basis and rules as the runs recorded in
  evaluation-strategy.md; the assistant asks before the call; the owner reviews the fixtures for
  sensitive content before they enter the repository.

## Known gaps / not solved

- Supervise overlaps the `AssessmentRecord` validator; the validator stays the last line and the
  verdict is the inspectable form. A failure both would refuse is still a 500 with nothing stored.
- `Retrieve` is a placeholder; the real stage lands with RAG stage 1 (ADR 0022) and must keep
  the "context only" rule.
- Supervise does not re-run its invariant checks after the second `reconcile`; the
  `AssessmentRecord` validator covers that case.
- The web "Rules alone" label (`web/app/assessments/[id]/page.tsx`) is renamed in the deferred
  web step unless done earlier; the API note already says "Deterministic checks".
- The stage trace records when a stage ran and what it used, not why a model said what it said;
  it is not a reasoning trace and must never become one.
- Trace timestamps are coarse: `started_at`/`finished_at` are the run's `now` and the one later
  clock reading taken in Rejudge, not per-stage measurements; `duration_ms` (monotonic, per
  stage) is the only per-stage timing and is likewise what the log line reports.
- Ablation measures this pipeline on synthetic, labelled cases; it does not measure real-world
  safety or a model's reliability (evaluation-strategy.md "Limits").
- The cross-examination round can still add noise or hostile-text-derived questions; the
  authority design bounds that to added caution, as for the first round (ADR 0007).
- The per-stage issue bound and the `stage_timeout + call_timeout` derivation are proposals for
  the reviewers, not validated limits.
- Duplicate JSON keys are accepted by the prompt layer: `json.loads` keeps the last value, so a
  model reply with a repeated key is validated on that value. No test yet. Not owner-approved.
- The AST guard that checks the pipeline passes no ablation switch does not cover indirect
  routes: `functools.partial`, `getattr`, `globals`, `sys.modules` or `exec`.
- The ablation switch patch is a module-global patch and is not safe across threads; run
  ablation single-threaded.
- Framework claims in the table are unverified vendor documentation; a later ADR that proposes a
  framework must verify them before relying on them.
