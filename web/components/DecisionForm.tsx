"use client";

import Link from "next/link";
import { useActionState, useState } from "react";
import { submitDecision, type FormState } from "../app/actions.ts";
import { isLessSevere, RECOMMENDATIONS } from "../lib/present.ts";

const initial: FormState = { error: null };
const MOST_SEVERE_FIRST = [...RECOMMENDATIONS].reverse();

export default function DecisionForm({
  id,
  system,
  context,
}: {
  id: string;
  system: string;
  context: string;
}) {
  const [state, action, pending] = useActionState(submitDecision, initial);
  const [choice, setChoice] = useState("");
  const known = (RECOMMENDATIONS as readonly string[]).includes(system);
  const lessSevere = known && isLessSevere(choice, system);
  return (
    <form action={action} className="card">
      <h2>Record the human decision</h2>
      <p>
        The system recommended <strong>{system}</strong>. {context}{" "}
        <Link href={`/assessments/${encodeURIComponent(id)}/evidence`}>Read the evidence first.</Link>
      </p>
      <p className="hint">
        You may accept it or choose a different outcome, including a less severe one. A different
        outcome needs a reason. This is recorded against your name and cannot be changed.
      </p>
      <input type="hidden" name="assessment_id" value={id} />
      <label htmlFor="decision">Your decision</label>
      <select
        id="decision"
        name="decision"
        value={choice}
        onChange={(e) => setChoice(e.target.value)}
        required
      >
        <option value="" disabled>Choose…</option>
        {MOST_SEVERE_FIRST.map((r) => (
          <option key={r} value={r}>{r}{r === system ? " (the system's recommendation)" : ""}</option>
        ))}
      </select>
      {lessSevere && (
        <p className="pending" role="alert">
          You are choosing a LESS SEVERE outcome than the system recommended ({system}). This will be
          recorded as an override against your name, with your reason.
        </p>
      )}
      <label htmlFor="rationale">Reason (required if different from the system)</label>
      <textarea id="rationale" name="rationale" rows={4} maxLength={5000} />
      {state.error && <p role="alert" className="error">{state.error}</p>}
      <button type="submit" disabled={pending}>{pending ? "Recording…" : "Record decision"}</button>
    </form>
  );
}
