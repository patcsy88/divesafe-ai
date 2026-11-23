"use client";

import { useActionState } from "react";
import { Button } from "@heroui/react/button";
import { Input } from "@heroui/react/input";
import { Label } from "@heroui/react/label";
import { TextArea } from "@heroui/react/textarea";
import { submitActualConditions, type FormState } from "../app/actions.ts";
import { NUMERIC_FIELDS } from "../lib/observations.ts";

const initial: FormState = { error: null };

export default function ConditionsForm({ id }: { id: string }) {
  const [state, action, pending] = useActionState(submitActualConditions, initial);
  return (
    <form action={action} className="ds-card">
      <h2>Report the conditions you found</h2>
      <p className="ds-hint">
        What you actually saw, after the dive. Leave a box empty if you did not measure it. These are
        your reports, kept with the record. They do not change the recommendation and can be sent once.
      </p>
      <input type="hidden" name="assessment_id" value={id} />
      {NUMERIC_FIELDS.map((f) => (
        <div key={f.key}>
          <Label htmlFor={f.key} className="ds-field-label">{f.label}</Label>
          <Input
            id={f.key}
            name={f.key}
            inputMode="decimal"
            autoComplete="off"
            fullWidth
            className="ds-field"
          />
        </div>
      ))}
      <Label htmlFor="notes" className="ds-field-label">Notes</Label>
      <TextArea
        id="notes"
        name="notes"
        rows={4}
        maxLength={2000}
        fullWidth
        className="ds-field"
      />
      {state.error && <p role="alert" className="ds-error">{state.error}</p>}
      <Button type="submit" isDisabled={pending} className="ds-submit">
        {pending ? "Recording…" : "Record conditions"}
      </Button>
    </form>
  );
}
