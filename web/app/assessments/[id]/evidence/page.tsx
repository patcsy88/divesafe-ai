import Link from "next/link";
import { notFound, redirect } from "next/navigation";
import Banner from "../../../../components/Banner.tsx";
import WithheldBox from "../../../../components/WithheldBox.ts";
import { ApiError, getEvidence } from "../../../../lib/api.ts";
import { requireKey } from "../../../../lib/auth.ts";
import { formatWhen, plain, provenanceLabel, shown } from "../../../../lib/present.ts";
import type { EvidenceView } from "../../../../lib/types.ts";

export default async function EvidencePage({ params }: { params: Promise<{ id: string }> }) {
  const key = await requireKey();
  const { id } = await params;
  let view: EvidenceView;
  try {
    view = await getEvidence(key, id);
  } catch (error) {
    if (error instanceof ApiError) {
      if (error.status === 401) redirect("/logout");
      if (error.status === 404) notFound();
      return <p role="alert" className="ds-error">{error.message}</p>;
    }
    throw error;
  }
  return (
    <>
      <header className="ds-top">
        <h1>Evidence</h1>
        <Link href={`/assessments/${encodeURIComponent(view.assessment_id)}`}>Back to the assessment</Link>
      </header>
      <Banner />
      <p className="ds-pending" role="note">{plain(view.evidence_note)}</p>
      <p className="ds-hint">
        Assessed at {formatWhen(view.assessed_at)}. Quality tags here do not say an item covers the
        dive window or is recent enough; that is not shown on this page.
      </p>

      {view.evidence_issues.length > 0 && (
        <section className="ds-card ds-warn">
          <h2>Sources that failed or were rejected</h2>
          <ul>{view.evidence_issues.map((i, n) => <li key={n}>{plain(i)}</li>)}</ul>
        </section>
      )}

      {view.unevaluated_factors.length > 0 && (
        <section className="ds-card ds-warn">
          <h2>Not evaluated</h2>
          <p>
            No validated limit exists for these, so they were not assessed. That is not the same as
            being safe.
          </p>
          <p>{view.unevaluated_factors.map(plain).join(", ")}</p>
        </section>
      )}

      <WithheldBox items={view.withheld_evidence} note={view.withheld_note} />

      {view.evidence.length === 0 && <p>No evidence was retrieved for this assessment.</p>}
      {view.evidence.map(({ item, effective_quality, retrieval_age_minutes_at_assessment, future_dated }) => (
        <section className="ds-card" key={item.id}>
          <h2>{plain(item.category)}</h2>
          <p>
            <span className="ds-tag">{plain(item.data_kind)}</span>
            <span className="ds-tag">{provenanceLabel(item.data_kind)}</span>
            {future_dated && <span className="ds-tag">dated after the assessment</span>}
            <span className="ds-tag">quality: {plain(effective_quality)}</span>
          </p>
          <dl>
            <dt>Source</dt>
            <dd>{plain(item.source)}{item.source_version ? ` (${plain(item.source_version)})` : ""}</dd>
            <dt>Retrieved</dt>
            <dd>
              {formatWhen(item.retrieved_at)}
              {future_dated
                ? " (dated after the assessment: treat with suspicion)"
                : retrieval_age_minutes_at_assessment !== null
                  ? `, ${plain(String(retrieval_age_minutes_at_assessment))} min before the assessment`
                  : ""}
            </dd>
            <dt>Refers to</dt>
            <dd>{formatWhen(item.valid_at)}{item.valid_until ? ` to ${formatWhen(item.valid_until)}` : ""}</dd>
            <dt>Derived from</dt>
            <dd>
              {item.upstream.length > 0 ? item.upstream.map(plain).join(", ") : "not stated"}
              {item.upstream_known ? "" : " (list not confirmed complete: not treated as independent of other sources)"}
            </dd>
          </dl>
          {item.quality_notes.length > 0 && (
            <ul>{item.quality_notes.map((n, i) => <li key={i}>{plain(n)}</li>)}</ul>
          )}
          <details>
            <summary>Raw values (third-party text, shown as plain text)</summary>
            <pre className="ds-value">{shown(item.value)}</pre>
          </details>
        </section>
      ))}
      <p className="ds-hint">{plain(view.notice)}</p>
    </>
  );
}
