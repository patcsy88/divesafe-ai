"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const EVERY_MS = 3_000;
// Longer than the API's own run deadline, so reaching this means the API did not answer either.
const GIVE_UP_AFTER_MS = 6 * 60_000;

// Asks the server to render this page again until the run has finished. It decides nothing: what
// the page shows is whatever the API says, and nothing here is a result.
export default function RunProgress() {
  const router = useRouter();
  const [gaveUp, setGaveUp] = useState(false);
  useEffect(() => {
    const started = Date.now();
    const timer = setInterval(() => {
      if (Date.now() - started > GIVE_UP_AFTER_MS) {
        clearInterval(timer);
        setGaveUp(true);
        return;
      }
      router.refresh();
    }, EVERY_MS);
    return () => clearInterval(timer);
  }, [router]);
  if (gaveUp) {
    return (
      <p role="alert" className="error">
        Still running; outcome unknown. Nothing has been assessed and nothing may be acted on.
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
