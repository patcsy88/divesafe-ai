"use client";

import { useActionState } from "react";
import { login, type FormState } from "../app/actions.ts";

const initial: FormState = { error: null };

export default function LoginForm() {
  const [state, action, pending] = useActionState(login, initial);
  return (
    <form action={action} className="card">
      <label htmlFor="api_key">Personal API key</label>
      <input id="api_key" name="api_key" type="password" autoComplete="off" required />
      <p className="hint">
        Each person uses their own key, so every decision is recorded against the right name. Keys
        are never shared or shown again.
      </p>
      {state.error && <p role="alert" className="error">{state.error}</p>}
      <button type="submit" disabled={pending}>{pending ? "Checking…" : "Sign in"}</button>
    </form>
  );
}
