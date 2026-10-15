// Waits between checks of a running assessment, growing to a cap so a slow run does not mean many
// requests.
export const DELAYS_MS: readonly number[] = [3_000, 3_000, 5_000, 5_000, 10_000, 15_000];

// A fixed limit on how long the page keeps asking. The API's own deadline can be longer if the
// operator set it so, in which case the run may still finish after the page gives up.
export const GIVE_UP_AFTER_MS = 6 * 60_000;

export function delayFor(round: number): number {
  const last = DELAYS_MS[DELAYS_MS.length - 1] ?? 15_000;
  return DELAYS_MS[Math.min(Math.max(0, Math.trunc(round) || 0), DELAYS_MS.length - 1)] ?? last;
}
