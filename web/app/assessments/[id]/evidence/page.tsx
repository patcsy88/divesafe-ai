import Link from "next/link";
import { notFound, redirect } from "next/navigation";
import Banner from "../../../../components/Banner.tsx";
import { ApiError, getEvidence } from "../../../../lib/api.ts";
import { requireKey } from "../../../../lib/auth.ts";
import { formatWhen, plain, provenanceLabel } from "../../../../lib/present.ts";
import type { EvidenceView } from "../../../../lib/types.ts";

const MAX_SHOWN = 20_000;

function shown(value: unknown): string {
  const text = JSON.stringify(value ?? null, null, 2) ?? "null";
  const clean = plain(text);
  return clean.length > MAX_SHOWN ? `${clean.slice(0, MAX_SHOWN)}\n… truncated` : clean;
}

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
      return <p role="alert" className="error">{error.message}</p>;
    }
    throw error;
  }
  return (
    <>
      <header className="top">
        <h1>Evidence</h1>
        <Link href={`/assessments/${encodeURIComponent(view.assessment_id)}`}>Back to the assessment</Link>
      </header>
      <Banner />
      <p className="pending" role="note">{plain(view.evidence_note)}</p>
      <p className="hint">
        Assessed at {formatWhen(view.assessed_at)}. Quality tags here do not say an item covers the
        dive window or is recent enough; that is not shown on this page.
      </p>

      {view.evidence_issues.length > 0 && (
        <section className="card warn">
          <h2>Sources that failed or were rejected</h2>
          <ul>{view.evidence_issues.map((i, n) => <li key={n}>{plain(i)}</li>)}</ul>
        </section>
      )}

      {view.unevaluated_factors.length > 0 && (
        <section className="card warn">
          <h2>Not evaluated</h2>
          <p>{view.unevaluated_factors.map(plain).join(", ")}</p>
        </section>
      )}

      {view.evidence.length === 0 && <p>No evidence was retrieved for this assessment.</p>}
      {view.evidence.map(({ item, effective_quality, retrieval_age_minutes_at_assessment, future_dated }) => (
        <section className="card" key={item.id}>
          <h2>{plain(item.category)}</h2>
          <p>
            <span className="tag">{plain(item.data_kind)}</span>
            <span className="tag">{provenanceLabel(item.data_kind)}</span>
            {future_dated && <span className="tag">dated after the assessment</span>}
            <span className="tag">quality: {plain(effective_quality)}</span>
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
            <pre className="value">{shown(item.value)}</pre>
          </details>
        </section>
      ))}
      <p className="hint">{plain(view.notice)}</p>
    </>
  );
}
