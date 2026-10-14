import { Fragment } from "react";
import Link from "next/link";
import { notFound, redirect } from "next/navigation";
import Banner from "../../../components/Banner.tsx";
import ConditionsForm from "../../../components/ConditionsForm.tsx";
import DecisionForm from "../../../components/DecisionForm.tsx";
import RunProgress from "../../../components/RunProgress.tsx";
import { ApiError, getAssessment } from "../../../lib/api.ts";
import { requireKey } from "../../../lib/auth.ts";
import { badgeClass, formatWhen, labelFor, plain } from "../../../lib/present.ts";
import { failureText as failedReason, type Fetched } from "../../../lib/status.ts";

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
      return <p role="alert" className="error">{error.message}</p>;
    }
    throw error;
  }
  if (fetched.kind === "run") {
    const run = fetched.view;
    const failed = run.status === "FAILED";
    return (
      <>
        <header className="top">
          <h1>Assessment</h1>
          <nav className="sub">
            <Link href="/">New assessment</Link>
          </nav>
        </header>
        <Banner />
        <section className="card">
          <p className="label">{failed ? "Did not finish" : "Running"}</p>
          <p className="pending" role="status">
            NO RESULT. {plain(run.status_note)}
          </p>
          {failed && <p>{failedReason(run.failure_code)}</p>}
          {failed ? (
            <p className="hint">
              This is not INSUFFICIENT EVIDENCE: nothing was assessed. Start a new assessment.
            </p>
          ) : (
            <RunProgress />
          )}
        </section>
        <p className="hint">{plain(run.notice)}</p>
      </>
    );
  }
  const view = fetched.view;
  const r = view.record;
  const pending = view.status === "PENDING_HUMAN";
  const decision = r.human_decision;
  const started = Date.parse(r.plan.planned_start) <= Date.now();

  return (
    <>
      <header className="top">
        <h1>Assessment</h1>
        <nav className="sub">
          <Link href={`/assessments/${encodeURIComponent(r.id)}/evidence`}>Evidence</Link>
          <Link href="/">New assessment</Link>
        </nav>
      </header>
      <Banner />

      <section className="card">
        <p className="label">System recommendation</p>
        <p className={badgeClass(r.final_recommendation)}>{labelFor(r.final_recommendation)}</p>
        {pending && (
          <p className="pending" role="status">
            PENDING HUMAN DECISION. {view.status_note}
          </p>
        )}
        <p>{view.outcome_note}</p>
        <dl>
          <dt>Rules alone</dt>
          <dd>{labelFor(r.deterministic_recommendation)}</dd>
          {r.proposed_recommendation && (
            <>
              <dt>Unverified model proposal</dt>
              <dd>
                {labelFor(r.proposed_recommendation)}
                {view.llm_tightened ? " (this made the result stricter)" : " (it could not relax the result)"}
              </dd>
            </>
          )}
          <dt>Confidence</dt>
          <dd>{view.confidence_note}</dd>
          <dt>Ruleset</dt>
          <dd>{r.ruleset_version}</dd>
        </dl>
      </section>

      {decision && (
        <section className="card">
          <h2>Human decision</h2>
          <dl>
            <dt>Decided by</dt>
            <dd>{plain(decision.decided_by)}</dd>
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
                <dd>{plain(decision.override_rationale)}</dd>
              </>
            )}
            <dt>When</dt>
            <dd>{formatWhen(decision.decided_at)}</dd>
          </dl>
        </section>
      )}

      {pending && r.assigned_decider && (
        <>
          <p className="hint">
            Only {plain(r.assigned_decider)} can record this decision. The service checks who you
            are; if you are someone else it will refuse and nothing is recorded.
          </p>
          <DecisionForm
            id={r.id}
            system={r.final_recommendation}
            context={`Rules alone: ${labelFor(r.deterministic_recommendation)}. ${r.evidence_issues.length} source issue(s); ${r.unevaluated_factors.length} factor(s) not evaluated.`}
          />
        </>
      )}

      {r.actual_conditions && (
        <section className="card">
          <h2>Conditions reported afterwards</h2>
          <p className="hint">
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

      {!pending && !r.actual_conditions &&
        (started ? (
          <ConditionsForm id={r.id} />
        ) : (
          <p className="hint">
            Conditions can be reported once the planned start time ({formatWhen(r.plan.planned_start)}) has passed.
          </p>
        ))}

      <section className="card">
        <h2>The dive plan</h2>
        <dl>
          <dt>Site</dt>
          <dd>{r.plan.site_id}</dd>
          <dt>Start</dt>
          <dd>{formatWhen(r.plan.planned_start)}</dd>
          <dt>Duration</dt>
          <dd>{r.plan.planned_duration_minutes} minutes</dd>
          <dt>Maximum depth</dt>
          <dd>{r.plan.max_depth_m} m</dd>
          <dt>Requested by</dt>
          <dd>{r.created_by ?? "unknown"}</dd>
          <dt>Decision to be recorded by</dt>
          <dd>{r.assigned_decider ?? "nobody (this record cannot be decided)"}</dd>
        </dl>
      </section>

      {r.evidence_issues.length > 0 && (
        <section className="card warn">
          <h2>Sources that failed or were rejected</h2>
          <ul>
            {r.evidence_issues.map((issue, i) => (
              <li key={i}>{plain(issue)}</li>
            ))}
          </ul>
        </section>
      )}

      {r.unevaluated_factors.length > 0 && (
        <section className="card warn">
          <h2>Not evaluated</h2>
          <p>
            No validated limit exists for these, so they were not assessed. That is not the same as
            being safe.
          </p>
          <ul>
            {r.unevaluated_factors.map((f) => (
              <li key={f}>{f}</li>
            ))}
          </ul>
        </section>
      )}

      <section className="card">
        <h2>Rule results</h2>
        <ul>
          {r.rule_results.map((rule, i) => (
            <li key={`${rule.rule_id}-${i}`}>
              <strong>{labelFor(rule.outcome)}</strong> · {rule.rule_id}
              {rule.factor ? ` · ${rule.factor}` : ""}
              <br />
              <span>{plain(rule.rationale)}</span>
            </li>
          ))}
        </ul>
      </section>

      {(r.findings.length > 0 || r.scenarios.length > 0 || r.explanation) && (
        <section className="card ai">
          <h2>Model-written commentary</h2>
          <p className="hint">
            Written by an AI model. Unverified, and it never changes the result except to make it
            stricter. Read it as plain commentary.
          </p>
          {r.findings.map((f, i) => (
            <p key={`f-${i}`}>
              <strong>{f.agent}:</strong> {plain(f.summary)}
            </p>
          ))}
          {r.scenarios.map((s, i) => (
            <p key={`s-${i}`}>
              <strong>{s.kind}:</strong> {plain(s.summary)}
            </p>
          ))}
          {r.explanation && <p>{plain(r.explanation)}</p>}
        </section>
      )}

      {r.agent_issues.length > 0 && (
        <section className="card warn">
          <h2>Model calls that failed</h2>
          <ul>
            {r.agent_issues.map((a, i) => (
              <li key={i}>{plain(a)}</li>
            ))}
          </ul>
        </section>
      )}

      {view.attributions.length > 0 && (
        <p className="hint">Data: {view.attributions.join("; ")}</p>
      )}
      <p className="hint">{view.notice}</p>
    </>
  );
}
