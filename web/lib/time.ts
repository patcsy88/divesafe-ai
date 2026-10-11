export type Zone = "UTC" | "+08:00";

const LOCAL = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;

// The API needs a timezone-aware instant. A bare local time is refused rather than guessed.
export function toAwareIso(local: string, zone: string): string | null {
  if (!LOCAL.test(local)) return null;
  if (zone !== "UTC" && zone !== "+08:00") return null;
  const iso = `${local}:00${zone === "UTC" ? "Z" : zone}`;
  return Number.isNaN(Date.parse(iso)) ? null : iso;
}

export function parsePositive(value: string, integer: boolean): number | null {
  if (!/^\d+(\.\d+)?$/.test(value.trim())) return null;
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return null;
  return integer && !Number.isInteger(n) ? null : n;
}
