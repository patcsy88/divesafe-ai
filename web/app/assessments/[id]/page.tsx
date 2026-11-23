import { Fragment } from "react";
import Link from "next/link";
import { notFound, redirect } from "next/navigation";
import Banner from "../../../components/Banner.tsx";
import ConditionsForm from "../../../components/ConditionsForm.tsx";
import DegradedEvidenceNote, { degradedContext } from "../../../components/DegradedEvidenceNote.ts";
import DecisionForm from "../../../components/DecisionForm.tsx";
import ProviderNotices from "../../../components/ProviderNotices.ts";
import RunProgress from "../../../components/RunProgress.tsx";
import { ApiError, getAssessment } from "../../../lib/api.ts";
import { requireKey } from "../../../lib/auth.ts";
import { badgeClass, formatWhen, isLessSevere, labelFor, plain } from "../../../lib/present.ts";
import { failureText as failedReason, type Fetched } from "../../../lib/status.ts";
import {
  DEGRADED_NOTE_MISSING,
  degradedNoteMissing,
  KNOWLEDGE_NOTE_MISSING,
  knowledgeText,
  reasonShown,
  withheldCount,
} from "../../../lib/notes.ts";

export default async function AssessmentPage({ params }: { params: Promise<{ id: string }> }) {
  const key = await requireKey();
  const { id } = await params;
  let fetched: Fetched;
  try {
    fetched = await getAssessment(key, id);
  } catch (error) {
    if (error instanceof ApiError) {
      if (error.status === 401) redirect("/logout");
      if (error.status === 404) notFound();
      return <p role="alert" className="ds-error">{error.message}</p>;
    }
    throw error;
  }
  if (fetched.kind === "run") {
    const run = fetched.view;
    const failed = run.status === "FAILED";
    return (
      <>
        <header className="ds-top">
          <h1>Assessment</h1>
          <nav className="ds-sub">
            <Link href="/">New assessment</Link>
          </nav>
        </header>
        <Banner />
        <section className="ds-card">
          <p className="ds-label">{failed ? "Did not finish" : "Running"}</p>
          {failed ? (
            <>
              <p className="ds-error" role="alert">
                NO RESULT. The assessment did not finish. Do not act on this page.
              </p>
              <p>{failedReason(run.failure_code)}</p>
              <p className="ds-hint">
                This is not a result. It does not mean conditions are acceptable, and it is not
                INSUFFICIENT EVIDENCE either: nothing was checked. Start a new assessment.
              </p>
            </>
          ) : (
            <>
              <p className="ds-pending" role="status">
                NO RESULT YET. Nothing has been assessed. Do not act on this page.
              </p>
              <RunProgress />
            </>
          )}
        </section>
        <p className="ds-hint">{plain(run.notice)}</p>
      </>
    );
  }
  const view = fetched.view;
  const r = view.record;
  const withheld = view.decision_withheld;
  const pending = view.status === "PENDING_HUMAN" && !withheld;
  const decision = r.human_decision;
  const started = Date.parse(r.plan.planned_start) <= Date.now();
  const knowledge = knowledgeText(view.knowledge_note);
  const setAside = withheldCount(r.withheld_evidence);
  const canDecide = pending && Boolean(r.assigned_decider);
  // Worded from the two checked outcomes, not from a flag.
  const proposalNote = isLessSevere(r.proposed_recommendation ?? "", r.deterministic_recommendation)
    ? " (less severe than the rules: discarded)"
    : r.proposed_recommendation === r.deterministic_recommendation
      ? " (same as the rules)"
      : " (this made the result stricter)";
  const knowledgeMissing = knowledge === KNOWLEDGE_NOTE_MISSING;
  const setAsideAction = withheld
    ? "Ask the assigned decider, who can read them on the Evidence page."
    : "Read them on the Evidence page before deciding.";
  // Worded from the outcomes and the less-severe flag, both checked by classify().
  const decidedNote = !decision
    ? ""
    : decision.decision === r.final_recommendation
      ? " (accepted the system's recommendation)"
      : view.overrides_to_less_severe
        ? " (an override to a LESS SEVERE outcome than the system recommended)"
        : " (an override of the system: more severe)";

  return (
    <>
      <header className="ds-top">
        <h1>Assessment</h1>
        <nav className="ds-sub">
          {!withheld && <Link href={`/assessments/${encodeURIComponent(r.id)}/evidence`}>Evidence</Link>}
          <Link href="/">New assessment</Link>
        </nav>
      </header>
      <Banner />

      <section className="ds-card">
        {pending && (
          <p className="ds-pending" role="status">
            PENDING HUMAN DECISION. {plain(view.status_note)}
          </p>
        )}
        {withheld && (
          <p className="ds-pending" role="status">
            DECISION NOT SHOWN TO THIS KEY. It does not mean none was made. Do not act on this
            result without asking the assigned decider.
          </p>
        )}
        {decision && (
          <p className="ds-pending" role="status">
            HUMAN DECISION RECORDED: {labelFor(decision.decision)}
            {plain(decidedNote)}.
            See the human decision below.
          </p>
        )}
        <p className="ds-label">
          {pending ? "System recommendation (not yet decided by a human)" : "System recommendation"}
        </p>
        <p className={badgeClass(r.final_recommendation)}>{labelFor(r.final_recommendation)}</p>
        <DegradedEvidenceNote note={view.degraded_evidence_note} />
        {degradedNoteMissing(r.final_recommendation, view.degraded_evidence_note) && (
          <p className="ds-pending" role="note">{plain(DEGRADED_NOTE_MISSING)}</p>
        )}
        {!pending && !withheld && view.status_note && <p>{plain(view.status_note)}</p>}
        <p>{plain(view.outcome_note)}</p>
        {view.agent_note && <p>{plain(view.agent_note)}</p>}
        {knowledgeMissing && (
          <p className="ds-pending" role="note">
            {plain(KNOWLEDGE_NOTE_MISSING)}
          </p>
        )}
        {knowledge && !knowledgeMissing && <p role="note">{plain(knowledge)}</p>}
        {canDecide && (
          <p className="ds-hint">
            Read the problems and evidence on this page first; the decision form is at the end.
          </p>
        )}
        <dl>
          <dt>Deterministic checks</dt>
          <dd>{labelFor(r.deterministic_recommendation)}</dd>
          {r.proposed_recommendation && (
            <>
              <dt>Unverified model proposal</dt>
              <dd>
                {labelFor(r.proposed_recommendation)}
                {plain(proposalNote)}
              </dd>
            </>
          )}
          <dt>Confidence</dt>
          <dd>{plain(view.confidence_note)}</dd>
          <dt>Ruleset</dt>
          <dd>{plain(r.ruleset_version)}</dd>
        </dl>
      </section>

      {decision && (
        <section className="ds-card">
          <h2>Human decision</h2>
          <dl>
            <dt>Decided by</dt>
            <dd>{plain(decision.decided_by)}</dd>
            <dt>Decided by the requester</dt>
            <dd>
              {plain(
                decision.decided_by_creator === true
                  ? "Yes: the person who requested this assessment also made this decision"
                  : decision.decided_by_creator === false
                    ? "No"
                    : "Not recorded",
              )}
            </dd>
            {typeof decision.key_id === "string" && decision.key_id && (
              <>
                <dt>Key used</dt>
                <dd>{plain(decision.key_id)}</dd>
              </>
            )}
            <dt>Decision</dt>
            <dd>{labelFor(decision.decision)}</dd>
            <dt>Override of the system</dt>
            <dd>
              {decision.is_override ? "Yes" : "No"}
              {view.overrides_to_less_severe && (
                <strong> · overrides to a LESS SEVERE outcome than the system recommended</strong>
              )}
            </dd>
            {decision.override_rationale && (
              <>
                <dt>Rationale</dt>
                <dd>{plain(reasonShown(decision.override_rationale))}</dd>
              </>
            )}
            <dt>When</dt>
            <dd>{formatWhen(decision.decided_at)}</dd>
          </dl>
        </section>
      )}

      {r.actual_conditions && (
        <section className="ds-card">
          <h2>Conditions reported afterwards</h2>
          <p className="ds-hint">
            Reported by {plain(r.actual_conditions.reported_by)} at{" "}
            {formatWhen(r.actual_conditions.reported_at)}. A report, not a measurement by the system.
          </p>
          <dl>
            {Object.entries(r.actual_conditions.observations).map(([k, v]) => (
              <Fragment key={k}>
                <dt>{plain(k)}</dt>
                <dd>{plain(String(v))}</dd>
              </Fragment>
            ))}
          </dl>
        </section>
      )}

      {!pending && !withheld && !r.actual_conditions &&
        (started ? (
          <ConditionsForm id={r.id} />
        ) : (
          <p className="ds-hint">
            Conditions can be reported once the planned start time ({formatWhen(r.plan.planned_start)}) has passed.
          </p>
        ))}

      <section className="ds-card">
        <h2>The dive plan</h2>
        <dl>
          <dt>Site</dt>
          <dd>{plain(r.plan.site_id)}</dd>
          <dt>Start</dt>
          <dd>{formatWhen(r.plan.planned_start)}</dd>
          <dt>Duration</dt>
          <dd>{plain(String(r.plan.planned_duration_minutes))} minutes</dd>
          <dt>Maximum depth</dt>
          <dd>{plain(String(r.plan.max_depth_m))} m</dd>
          <dt>Requested by</dt>
          <dd>{r.created_by ? plain(r.created_by) : "unknown"}</dd>
          <dt>Decision to be recorded by</dt>
          <dd>{r.assigned_decider ? plain(r.assigned_decider) : "nobody (this record cannot be decided)"}</dd>
          {(r.reassignments ?? []).length > 0 && <dt>Moved between deciders</dt>}
          {(r.reassignments ?? []).length > 0 && (
            <dd>
              <ul>
                {(r.reassignments ?? []).map((m, i) => (
                  <li key={i}>
                    {plain(m.from_decider)} to {plain(m.to_decider)}, by {plain(m.reassigned_by)} on{" "}
                    {formatWhen(m.reassigned_at)} (key {plain(m.key_id)})
                  </li>
                ))}
              </ul>
            </dd>
          )}
        </dl>
      </section>

      {r.evidence_issues.length > 0 && (
        <section className="ds-card ds-warn">
          <h2>Sources that failed or were rejected</h2>
          <ul>
            {r.evidence_issues.map((issue, i) => (
              <li key={i}>{plain(issue)}</li>
            ))}
          </ul>
        </section>
      )}

      {setAside > 0 && (
        <section className="ds-card ds-warn">
          <h2>Conflicting evidence set aside</h2>
          <p>
            {plain(String(setAside))} conflicting item(s) were set aside before the rules ran. They may hold a more
            severe result than the one shown.{" "}
            {plain(setAsideAction)}
          </p>
        </section>
      )}

      {r.unevaluated_factors.length > 0 && (
        <section className="ds-card ds-warn">
          <h2>Not evaluated</h2>
          <p>
            No validated limit exists for these, so they were not assessed. That is not the same as
            being safe.
          </p>
          <ul>
            {r.unevaluated_factors.map((f) => (
              <li key={f}>{plain(f)}</li>
            ))}
          </ul>
        </section>
      )}

      <section className="ds-card">
        <h2>Rule results</h2>
        <ul>
          {r.rule_results.map((rule, i) => (
            <li key={`${rule.rule_id}-${i}`}>
              rule outcome: <strong>{labelFor(rule.outcome)}</strong> · {plain(rule.rule_id)}
              {rule.factor ? ` · ${plain(rule.factor)}` : ""}
              <br />
              <span>{plain(rule.rationale)}</span>
            </li>
          ))}
        </ul>
      </section>

      {(r.findings.length > 0 || r.scenarios.length > 0 || r.explanation) && (
        <section className="ds-card ds-ai">
          <h2>Model-written commentary</h2>
          <p className="ds-hint">
            Written by an AI model. Unverified, and it never changes the result except to make it
            stricter. Read it as plain commentary.
          </p>
          {r.findings.map((f, i) => (
            <p key={`f-${i}`}>
              <strong>{plain(f.agent)}:</strong> {plain(f.summary)}
              {f.no_evidence ? " (cites no evidence)" : ""}
            </p>
          ))}
          {r.scenarios.map((s, i) => (
            <p key={`s-${i}`}>
              <strong>{plain(s.kind)}:</strong> {plain(s.summary)}
            </p>
          ))}
          {r.explanation && <p>{plain(r.explanation)}</p>}
        </section>
      )}

      {[...r.findings, ...r.cross_examination_findings, ...r.scenarios].some(
        (x) => x.conflicting_signals.length > 0,
      ) && (
        <section className="ds-card ds-ai">
          <h2>Questions and conflicting signals (model-written)</h2>
          <p className="ds-hint">
            Written by an AI model and not verified. An automatic wording screen marks some lines
            FLAGGED. It misses wording, and records made before 2026-11-03 were not screened. An
            unmarked line has not been checked or endorsed. None of it changes the result; you
            decide on safety.
          
            {r.model_version?.includes("; signal_screen=")
              ? " This record was made with the wording screen."
              : " This record was made before the wording screen existed and was not screened."}
          </p>
          <ul>
            {r.findings.flatMap((f, i) =>
              f.conflicting_signals.map((c, j) => (
                <li key={`c-${i}-${j}`}>
                  <strong>{plain(f.agent)}:</strong> {plain(c)}
                  {f.flagged_signals?.includes(j) ? " (flagged by the wording screen)" : ""}
                </li>
              )),
            )}
            {r.cross_examination_findings.flatMap((f, i) =>
              f.conflicting_signals.map((c, j) => (
                <li key={`x-${i}-${j}`}>
                  <strong>{plain(f.agent)} (second round):</strong> {plain(c)}
                  {f.flagged_signals?.includes(j) ? " (flagged by the wording screen)" : ""}
                </li>
              )),
            )}
            {r.scenarios.flatMap((s, i) =>
              s.conflicting_signals.map((c, j) => (
                <li key={`sc-${i}-${j}`}>
                  <strong>{plain(s.kind)}:</strong> {plain(c)}
                  {s.flagged_signals?.includes(j) ? " (flagged by the wording screen)" : ""}
                </li>
              )),
            )}
          </ul>
        </section>
      )}

      {r.agent_issues.length > 0 && (
        <section className="ds-card ds-warn">
          <h2>Model calls that failed, and model-server notes</h2>
          <ul>
            {r.agent_issues.map((a, i) => (
              <li key={i}>{plain(a)}</li>
            ))}
          </ul>
        </section>
      )}

      {canDecide && (
        <>
          <p className="ds-hint">
            Only {plain(r.assigned_decider ?? "")} can record this decision. The service checks who you
            are; if you are someone else it will refuse and nothing is recorded.
          </p>
          <DecisionForm
            id={r.id}
            system={r.final_recommendation}
            context={`Deterministic checks: ${labelFor(r.deterministic_recommendation)}. ${r.evidence_issues.length} source issue(s); ${r.unevaluated_factors.length} factor(s) not evaluated; ${setAside} conflicting item(s) set aside (see Evidence).${degradedContext(view.degraded_evidence_note)}${degradedNoteMissing(r.final_recommendation, view.degraded_evidence_note) ? ` ${DEGRADED_NOTE_MISSING}` : ""}${knowledgeMissing ? ` ${KNOWLEDGE_NOTE_MISSING}` : ""}`}
          />
        </>
      )}

      {view.attributions.length > 0 && (
        <p className="ds-hint">
          Data attribution (supplied by the provider, not checked): {plain(view.attributions.join("; "))}
        </p>
      )}
      <ProviderNotices
        notices={view.provider_notices}
        notShown={view.provider_notices_not_shown}
        missing={view.provider_notice_missing}
      />
      <p className="ds-hint">{plain(view.notice)}</p>
    </>
  );
}
