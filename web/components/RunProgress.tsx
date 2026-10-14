"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, useTransition } from "react";

// Waits between checks, growing to a cap so a slow run does not mean many requests.
const DELAYS_MS = [3_000, 3_000, 5_000, 5_000, 10_000, 15_000];
// A fixed limit on how long this page keeps asking. The API's own deadline can be longer if the
// operator set it so, in which case the run may still finish after this page gives up.
const GIVE_UP_AFTER_MS = 6 * 60_000;

// Asks the server to render this page again until the run has finished. It decides nothing: what
// the page shows is whatever the API says, and nothing here is a result.
export default function RunProgress() {
  const router = useRouter();
  const [gaveUp, setGaveUp] = useState(false);
  const [busy, startRefresh] = useTransition();
  // Counts checks made, and also waits skipped while the tab was hidden, so the timer restarts.
  const [round, setRound] = useState(0);

  useEffect(() => {
    const timer = setTimeout(() => setGaveUp(true), GIVE_UP_AFTER_MS);
    return () => clearTimeout(timer);
  }, []);

  useEffect(() => {
    // One refresh at a time, so a slow API is not asked again before it has answered.
    if (gaveUp || busy) return;
    const delay = DELAYS_MS[Math.min(round, DELAYS_MS.length - 1)];
    const timer = setTimeout(() => {
      if (document.hidden) {
        setRound((n) => n + 1);
        return;
      }
      startRefresh(() => {
        router.refresh();
      });
      setRound((n) => n + 1);
    }, delay);
    return () => clearTimeout(timer);
  }, [busy, gaveUp, round, router]);

  if (gaveUp) {
    return (
      <p role="alert" className="error">
        No answer yet; outcome unknown. It may have finished or failed. Nothing may be acted on.
        Reload this page to check again.
      </p>
    );
  }
  return (
    <p className="hint" role="status">
      Checking again every few seconds…
    </p>
  );
}
