// What the person saw. These are reports, not limits, and nothing here is compared to a threshold.
export const NUMERIC_FIELDS: ReadonlyArray<{ key: string; label: string }> = [
  { key: "wave_height_m", label: "Wave height (metres)" },
  { key: "visibility_m", label: "Visibility (metres)" },
  { key: "water_temperature_c", label: "Water temperature (°C)" },
  { key: "current_knots", label: "Current (knots)" },
  { key: "wind_knots", label: "Wind (knots)" },
];

export type Parsed = { ok: true; observations: Record<string, string | number> } | { ok: false; error: string };

export function parseObservations(get: (name: string) => string): Parsed {
  const observations: Record<string, string | number> = {};
  for (const { key, label } of NUMERIC_FIELDS) {
    const raw = get(key).trim();
    if (raw === "") continue;
    const pattern = key === "water_temperature_c" ? /^-?\d+(\.\d+)?$/ : /^\d+(\.\d+)?$/;
    if (!pattern.test(raw) || !Number.isFinite(Number(raw))) {
      return { ok: false, error: `${label} must be a number${key === "water_temperature_c" ? "" : ", zero or more"}.` };
    }
    observations[key] = Number(raw);
  }
  const notes = get("notes").trim();
  if (notes.length > 2000) return { ok: false, error: "Notes are too long (2000 characters at most)." };
  if (/[^\P{Cc}\n\t]|\p{Cf}/u.test(notes)) {
    return { ok: false, error: "Notes contain control characters." };
  }
  if (notes) observations.notes = notes;
  if (Object.keys(observations).length === 0) {
    return { ok: false, error: "Enter at least one observation or a note." };
  }
  return { ok: true, observations };
}
