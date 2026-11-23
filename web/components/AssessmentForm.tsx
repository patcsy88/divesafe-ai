"use client";

import { useActionState, useEffect } from "react";
import { Button } from "@heroui/react/button";
import { Input } from "@heroui/react/input";
import { Label } from "@heroui/react/label";
import { runAssessment, type FormState } from "../app/actions.ts";
import { useRetryKey } from "./useRetryKey.ts";

const initial: FormState = { error: null };

export default function AssessmentForm({
  sites,
}: {
  sites: ReadonlyArray<{ id: string; label: string }>;
}) {
  const [state, action, pending] = useActionState(runAssessment, initial);
  const [retryKey, renewKey] = useRetryKey();
  useEffect(() => {
    if (state.renewKey) renewKey();
  }, [state, renewKey]);
  return (
    <form action={action} onChange={renewKey} className="ds-card">
      <input type="hidden" name="retry_key" value={retryKey} />
      <Label htmlFor="site_id" className="ds-field-label">Dive site</Label>
      <select id="site_id" name="site_id" required>
        {sites.map((s) => (
          <option key={s.id} value={s.id}>{s.label}</option>
        ))}
      </select>

      <Label htmlFor="planned_start" className="ds-field-label">Planned start</Label>
      <div className="ds-row">
        <Input
          id="planned_start"
          name="planned_start"
          type="datetime-local"
          required
          fullWidth
          className="ds-field"
        />
        <select name="zone" aria-label="Time zone" defaultValue="+08:00">
          <option value="+08:00">Malaysia time (UTC+8)</option>
          <option value="UTC">UTC</option>
        </select>
      </div>

      <Label htmlFor="duration_minutes" className="ds-field-label">
        Planned duration (minutes)
      </Label>
      <Input
        id="duration_minutes"
        name="duration_minutes"
        inputMode="numeric"
        required
        fullWidth
        className="ds-field"
      />

      <Label htmlFor="max_depth_m" className="ds-field-label">Maximum depth (metres)</Label>
      <Input
        id="max_depth_m"
        name="max_depth_m"
        inputMode="decimal"
        required
        fullWidth
        className="ds-field"
      />

      <Label htmlFor="decider" className="ds-field-label">
        Person who will record the decision (optional)
      </Label>
      <Input
        id="decider"
        name="decider"
        maxLength={100}
        autoComplete="off"
        fullWidth
        className="ds-field"
      />
      <p className="ds-hint">
        Leave blank to decide it yourself, if your key has the decider role. Otherwise name a key
        holder who does.
      </p>

      {state.error && <p role="alert" className="ds-error">{state.error}</p>}
      <Button type="submit" isDisabled={pending} className="ds-submit">
        {pending ? "Starting…" : "Run assessment"}
      </Button>
    </form>
  );
}
