"use client";

import { useActionState } from "react";
import { runAssessment, type FormState } from "../app/actions.ts";
import { SITES } from "../lib/sites.ts";

const initial: FormState = { error: null };

export default function AssessmentForm() {
  const [state, action, pending] = useActionState(runAssessment, initial);
  return (
    <form action={action} className="card">
      <label htmlFor="site_id">Dive site</label>
      <select id="site_id" name="site_id" required>
        {SITES.map((s) => (
          <option key={s.id} value={s.id}>{s.label}</option>
        ))}
      </select>

      <label htmlFor="planned_start">Planned start</label>
      <div className="row">
        <input id="planned_start" name="planned_start" type="datetime-local" required />
        <select name="zone" aria-label="Time zone" defaultValue="+08:00">
          <option value="+08:00">Malaysia time (UTC+8)</option>
          <option value="UTC">UTC</option>
        </select>
      </div>

      <label htmlFor="duration_minutes">Planned duration (minutes)</label>
      <input id="duration_minutes" name="duration_minutes" inputMode="numeric" required />

      <label htmlFor="max_depth_m">Maximum depth (metres)</label>
      <input id="max_depth_m" name="max_depth_m" inputMode="decimal" required />

      <label htmlFor="decider">Person who will record the decision (optional)</label>
      <input id="decider" name="decider" maxLength={100} autoComplete="off" />
      <p className="hint">
        Leave blank to decide it yourself, if your key has the decider role. Otherwise name a key
        holder who does.
      </p>

      {state.error && <p role="alert" className="error">{state.error}</p>}
      <button type="submit" disabled={pending}>
        {pending ? "Assessing… this can take a minute" : "Run assessment"}
      </button>
    </form>
  );
}
