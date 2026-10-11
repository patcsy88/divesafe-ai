"use client";

import { useActionState } from "react";
import { submitActualConditions, type FormState } from "../app/actions.ts";
import { NUMERIC_FIELDS } from "../lib/observations.ts";

const initial: FormState = { error: null };

export default function ConditionsForm({ id }: { id: string }) {
  const [state, action, pending] = useActionState(submitActualConditions, initial);
  return (
    <form action={action} className="card">
      <h2>Report the conditions you found</h2>
      <p className="hint">
        What you actually saw, after the dive. Leave a box empty if you did not measure it. These are
        your reports, kept with the record. They do not change the recommendation and can be sent once.
      </p>
      <input type="hidden" name="assessment_id" value={id} />
      {NUMERIC_FIELDS.map((f) => (
        <div key={f.key}>
          <label htmlFor={f.key}>{f.label}</label>
          <input id={f.key} name={f.key} inputMode="decimal" autoComplete="off" />
        </div>
      ))}
      <label htmlFor="notes">Notes</label>
      <textarea id="notes" name="notes" rows={4} maxLength={2000} />
      {state.error && <p role="alert" className="error">{state.error}</p>}
      <button type="submit" disabled={pending}>{pending ? "Recording…" : "Record conditions"}</button>
    </form>
  );
}
