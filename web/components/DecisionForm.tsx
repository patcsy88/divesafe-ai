"use client";

import Link from "next/link";
import { useActionState, useEffect, useState } from "react";
import { submitDecision, type FormState } from "../app/actions.ts";
import { isLessSevere, RECOMMENDATIONS } from "../lib/present.ts";
import { useRetryKey } from "./useRetryKey.ts";

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
  const known = (RECOMMENDATIONS as readonly string[]).includes(system);
  const [choice, setChoice] = useState("");
  const [reason, setReason] = useState("");
  const [reviewing, setReviewing] = useState(false);
  const [retryKey, renewKey] = useRetryKey();
  useEffect(() => {
    if (state.renewKey) {
      setReviewing(false);
      renewKey();
    }
  }, [state, renewKey]);
  const needsReason = known && choice !== "" && choice !== system && reason.trim() === "";
  const change = () => {
    setReviewing(false);
    renewKey();
  };
  const lessSevere = known && isLessSevere(choice, system);
  return (
    <form action={action} onChange={change} className="card">
      <input type="hidden" name="retry_key" value={retryKey} />
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
      <textarea
        id="rationale"
        name="rationale"
        rows={4}
        maxLength={5000}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
      />
      {state.error && <p role="alert" className="error">{state.error}</p>}
      {reviewing ? (
        <div className="card" role="group" aria-label="Confirm your decision">
          <p>
            <strong>Check before you record.</strong> You are recording{" "}
            <strong>{choice}</strong>
            {known && choice === system ? " (the system's recommendation)" : ` (the system recommended ${system})`}.
          </p>
          {reason.trim() && <p className="hint">Your reason: {reason.trim()}</p>}
          <p className="hint">This is recorded against your name and cannot be changed.</p>
          {needsReason && (
            <p className="error" role="alert">A different outcome needs a reason. Go back and add one.</p>
          )}
          <button type="submit" disabled={pending || needsReason}>
            {pending ? "Recording…" : `Confirm and record ${choice}`}
          </button>{" "}
          <button type="button" onClick={() => setReviewing(false)} disabled={pending}>
            Back and edit
          </button>
        </div>
      ) : (
        <button type="button" disabled={choice === ""} onClick={() => setReviewing(true)}>
          Review decision
        </button>
      )}
    </form>
  );
}
