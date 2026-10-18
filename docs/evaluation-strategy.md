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
professional. Raw records are kept outside the repository. They show the pipeline works with
whatever model the host served under that name; they do not show a model is safe or reliable.

**2026-10-17, `claude-sonnet-5-5` through the OpenAI-compatible adapter** (a local proxy at
`localhost:8317`, which forwards off-machine; code `cc3fa76`, prompt `2026-10-17.2`). That run
predates ADR 0023: the proxy was treated as local and no opt-in was required. What left the
machine was the synthetic scenarios plus recorded fixtures, including the recorded PHN tide item,
which went on to a third-party vendor while PHN's terms are not on file (ADR 0021, open). Which
model answered rests on the proxy's routing; no digest was recorded. Today the evaluation runs
the same gate as start-up and skips a loopback server unless `DIVESAFE_LLM_SERVER_IS_LOCAL=true`
or `DIVESAFE_ALLOW_EXTERNAL_LLM=true` is set; a forwarding proxy should use the latter. Each
output file now records the `egress_basis` and the host name. The tide check also skips
(writing a record that says so) unless the basis is `declared_local` or the operator sets
`DIVESAFE_EVAL_ACCEPT_PHN_TERMS_OPEN=true`, because the PHN item would otherwise leave the machine
while PHN's terms are not on file. A skipped tide check means that check did not run, and a run
record should say so.

**2026-10-18, `claude-sonnet-5-5` through the OpenAI-compatible adapter** at
`https://wall-e.ch30ng.org/v1` (run 3; code `2872b86`, prompt `2026-10-17.2`, `egress_basis`
`remote_external_opt_in`, run by the owner in their own shell). The host is on a public address
and its response header names are consistent with a routing proxy, so prompts probably go on to a
hosted model; that is inferred, not confirmed (ADR 0023). The same data left the machine as in run 2, including the
recorded PHN tide item, while PHN's terms are still not on file (ADR 0021, open). Which model
answered rests on the host's routing; no digest was recorded.

| Setting | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| Output tokens / call timeout / stage timeout | 1500 / 60 s / 150 s | 3000 / 120 s / 300 s | 3000 / 120 s / 300 s |
| Rule outcome accuracy | 1.0 | 1.0 | 1.0 |
| Under-severe results, downgrade attempts | 0, 0 | 0, 0 | 0, 0 |
| Proposals obtained (of 7 cases) | 5 | 7 | 7 |
| Schema-failure rate | 0.556 | 0.032 | 0.032 |
| Over-severe cases (model stricter than the synthetic rule) | 1 | 2 | 2 |
| Injection echoes (marker screen) | 1 | 1 | 0 |
| Tide check | ran | ran | ran (acknowledged, basis `remote_external_opt_in`) |

- Run 1's failures were almost all "output stopped at the token limit"; raising the run setting
  removed them. Run 2 left two refused replies in 63 calls (one schema rejection, one token-limit
  stop). The record keeps no raw text for a refused reply, so the cause of the schema rejection is
  unknown. Run 1 also had one provider error, which did not recur.
- Both screen hits for injection were the model reporting the injected instruction and ignoring
  it; the screen cannot tell a report from an echo.
- Run 3 matched run 2 on every figure in the table except the injection screen (0 hits, against
  1). "Matched" means the totals are equal, not that the same cases were involved (the two
  over-severe cases may differ). One run each cannot show why the screen differs: a difference in
  the model, ordinary variation, or different routing behind the host (neither run recorded which
  model answered). The screen cannot tell a report of an injection from an echo, so zero hits may
  only mean the model did not mention the injected text.
- Tide-prompt check (one recorded PHN tide item, ADR 0021): the result stayed INSUFFICIENT EVIDENCE
  in all three runs, and read by hand, no reply inferred a current, slack water or flow timing
  from the tide data (the replies that mention them say the tide data gives none). In run 3 the
  marker screen reported five hits (`current speed`, `flow timing`, `slack`, `tidal current`,
  `tidal stream`); every occurrence in the findings, scenarios and explanation was a statement
  that no current evidence exists or that tide heights cannot give it, and the model made no
  downgrade attempt and had no agent issues. For run 3 the hand reading was done by the author of
  the code, not independently; for runs 1 and 2 the same author read the replies and no raw-record
  hashes were taken (the files are held outside the repository). The raw records are outside the repository; their SHA-256 values are
  `024e04905f3d465e140616535c2f54f8ff793847b4e012c477fedb6da635bb4c` (evaluation) and
  `48017ad74d2b229bdebe0bb2ff78d3f5aa961eec55d02953fc827d483f44f113` (tide check), so a reviewer
  who is given the files can confirm they are the ones read.
- Not measured: other models, other prompts, repeat runs, paraphrased injection, invented hazards
  in well-formed replies (a known gap).

## Limits

Scenario results do not prove real-world safety. Evaluation supports the capstone's claims
about engineering quality, not operational certification.
