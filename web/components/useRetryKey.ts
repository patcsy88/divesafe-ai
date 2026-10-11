"use client";

import { useCallback, useEffect, useState } from "react";

// 32 hex characters. randomUUID needs a secure context (https or localhost), so fall back to
// getRandomValues; with neither, no key is sent and the request simply is not retry-safe.
function fresh(): string {
  const c = globalThis.crypto;
  if (c?.randomUUID) return c.randomUUID().replaceAll("-", "");
  if (c?.getRandomValues) {
    return Array.from(c.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
  }
  return "";
}

// One key per attempt, made in the browser after the first render (not during server rendering, so
// there is no mismatch). Submitting again without changing anything reuses it, so a request whose
// outcome was unknown is not repeated as a new one; changing any field starts a new attempt.
export function useRetryKey(): [string, () => void] {
  const [key, setKey] = useState("");
  useEffect(() => setKey(fresh()), []);
  const renew = useCallback(() => setKey(fresh()), []);
  return [key, renew];
}
