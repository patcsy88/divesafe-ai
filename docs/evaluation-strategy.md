# Evaluation strategy

## Layers

| Layer | Location | What it proves | Gate |
| --- | --- | --- | --- |
| Safety invariants | `tests/safety` | Hierarchy, severity order, insufficient-evidence handling | Must pass; never skipped |
| Unit | `tests/unit` | Domain, config, engine, providers | CI |
| Agent | `tests/agent` | Agent behavior with scripted `FakeProvider` output | CI |
| Integration | `tests/integration` | Database/pgvector, connectors (recorded fixtures) | CI where services exist |
| Evaluation | `tests/evaluation` | Quality metrics on labelled scenarios | Offline harness tests gate in CI; live model quality numbers are reported, not gated |

## Safety evaluation (highest priority)

- **Adversarial proposals:** an LLM/agent proposal less severe than the rules result never
  lowers the final result (property tests over all severity pairs already exist).
- **Prompt injection:** retrieved text and connector payloads containing instructions do not
  change behavior.
- **Evidence gaps:** removing any required category yields `INSUFFICIENT EVIDENCE`.
- **Provider swap:** same deterministic inputs give the same final recommendation across providers.
- **Contradiction:** conflicting sources lower confidence and are surfaced, never silently resolved.

## Quality evaluation

- **Scenario set:** curated, provenance-labelled cases (site, plan, evidence snapshot, expected
  rule outcome), reviewed by a qualified diving professional. Synthetic cases are labelled synthetic.
- **Metrics:**
  - rule-outcome accuracy on the scenario set (deterministic; should be exact);
  - citation validity: share of claims whose cited evidence supports them;
  - confidence calibration against outcomes (reliability curves) once data exists;
  - conservative-error rate: cases where the final recommendation was less severe than the reference;
  - abstention quality: correct use of `INSUFFICIENT EVIDENCE`;
  - retrieval recall/precision for RAG;
  - explanation faithfulness (human-rated sample).
- **Primary risk metric:** under-severe errors are weighted far above over-severe ones.

## Human-in-the-loop and learning

- Track acceptance vs override rate and override rationales to find rule or UX gaps.
- Track the less severe override rate (`overrides_to_less_severe`), split by `NO-GO` and by
  other recommendations, and review those rationales. See ADR 0004.
- Compare recommendations with submitted actual conditions to measure forecast reliability.
- Findings produce reviewed rule/model change proposals, never automatic changes.

## Reproducibility

Each evaluation run records the ruleset, model, prompt, data and code versions; LLM
evaluations use temperature 0 where supported and store raw outputs.

## Live runs recorded

Each is a single run at temperature 0 on synthetic scenarios, not reviewed by a diving
professional. Raw records are kept outside the repository. They show the pipeline works with that
model; they do not show a model is safe or reliable.

**2026-10-17, `claude-sonnet-5-5` through the OpenAI-compatible adapter** (a local proxy at
`localhost:8317`, which forwards off-machine; code `cc3fa76`, prompt `2026-10-17.2`).

| Setting | Run 1 | Run 2 |
|---|---|---|
| Output tokens / call timeout / stage timeout | 1500 / 60 s / 150 s | 3000 / 120 s / 300 s |
| Rule outcome accuracy | 1.0 | 1.0 |
| Under-severe results, downgrade attempts | 0, 0 | 0, 0 |
| Proposals obtained (of 7 cases) | 5 | 7 |
| Schema-failure rate | 0.556 | 0.032 |
| Over-severe cases (model stricter than the synthetic rule) | 1 | 2 |
| Injection echoes (marker screen) | 1 | 1 |

- Run 1's failures were almost all "output stopped at the token limit"; raising the run setting
  removed them. Run 2 left two refused replies in 63 calls (one schema rejection, one token-limit
  stop). The record keeps no raw text for a refused reply, so the cause of the schema rejection is
  unknown. Run 1 also had one provider error, which did not recur.
- Both screen hits for injection were the model reporting the injected instruction and ignoring
  it; the screen cannot tell a report from an echo.
- Tide-prompt check (one recorded PHN tide item, ADR 0021): the result stayed INSUFFICIENT EVIDENCE
  in both runs, and read by hand, no reply inferred a current, slack water or flow timing from
  the tide data (the replies that mention them say the tide data gives none).
- Not measured: other models, other prompts, repeat runs, paraphrased injection, invented hazards
  in well-formed replies (a known gap).

## Limits

Scenario results do not prove real-world safety. Evaluation supports the capstone's claims
about engineering quality, not operational certification.
