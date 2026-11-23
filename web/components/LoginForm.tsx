"use client";

import { useActionState } from "react";
import { Button } from "@heroui/react/button";
import { Input } from "@heroui/react/input";
import { Label } from "@heroui/react/label";
import { login, type FormState } from "../app/actions.ts";

const initial: FormState = { error: null };

export default function LoginForm() {
  const [state, action, pending] = useActionState(login, initial);
  return (
    <form action={action} className="ds-card">
      <Label htmlFor="api_key" className="ds-field-label">Personal API key</Label>
      <Input
        id="api_key"
        name="api_key"
        type="password"
        autoComplete="off"
        required
        fullWidth
        className="ds-field"
      />
      <p className="ds-hint">
        Each person uses their own key, so every decision is recorded against the right name. Keys
        are never shared or shown again.
      </p>
      {state.error && <p role="alert" className="ds-error">{state.error}</p>}
      <Button type="submit" isDisabled={pending} className="ds-submit">
        {pending ? "Checking…" : "Sign in"}
      </Button>
    </form>
  );
}
