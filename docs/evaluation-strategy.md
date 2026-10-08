# Evaluation strategy

## Layers

| Layer | Location | What it proves | Gate |
| --- | --- | --- | --- |
| Safety invariants | `tests/safety` | Hierarchy, severity order, insufficient-evidence handling | Must pass; never skipped |
| Unit | `tests/unit` | Domain, config, engine, providers | CI |
| Agent | `tests/agent` | Agent behavior with scripted `FakeProvider` output | CI |
| Integration | `tests/integration` | Database/pgvector, connectors (recorded fixtures) | CI where services exist |
| Evaluation | `tests/evaluation` | Quality metrics on labelled scenarios | Reported, tracked over time |

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
- Compare recommendations with submitted actual conditions to measure forecast reliability.
- Findings produce reviewed rule/model change proposals, never automatic changes.

## Reproducibility

Each evaluation run records the ruleset, model, prompt, data and code versions; LLM
evaluations use temperature 0 where supported and store raw outputs.

## Limits

Scenario results do not prove real-world safety. Evaluation supports the capstone's claims
about engineering quality, not operational certification.
