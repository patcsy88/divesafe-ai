// What the person saw. These are reports, not limits, and nothing here is compared to a threshold.
export const NUMERIC_FIELDS: ReadonlyArray<{ key: string; label: string }> = [
  { key: "wave_height_m", label: "Wave height (metres)" },
  { key: "visibility_m", label: "Visibility (metres)" },
  { key: "water_temperature_c", label: "Water temperature (°C)" },
  { key: "current_knots", label: "Current (knots)" },
  { key: "wind_knots", label: "Wind (knots)" },
];

// Control and format characters (bidi overrides, zero-width marks) other than a line break or a
// tab, line and paragraph separators, private-use and unassigned code points, characters Unicode
// says to draw as nothing (the Hangul fillers among them) and the blank braille pattern. Refused
// in text the person types, which is stored as audit text.
export const BLANK_BRAILLE = String.fromCodePoint(0x2800);
const UNSAFE_SOURCE =
  `[^\\P{Cc}\\n\\t]|[\\p{Cf}\\p{Zl}\\p{Zp}\\p{Co}\\p{Cn}\\p{Default_Ignorable_Code_Point}${BLANK_BRAILLE}]`;
export const UNSAFE_TEXT = new RegExp(UNSAFE_SOURCE, "u");

// A name is one line: none of the above, and no line break or tab either.
export const UNSAFE_NAME = new RegExp(`${UNSAFE_SOURCE}|[\\n\\t]`, "u");

// A browser sends textarea line breaks as CRLF; normalise them before checking or comparing.
export const normaliseNewlines = (s: string): string => s.replace(/\r\n?/g, "\n");

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
  const notes = normaliseNewlines(get("notes")).trim();
  if (notes.length > 2000) return { ok: false, error: "Notes are too long (2000 characters at most)." };
  if (UNSAFE_TEXT.test(notes)) {
    return { ok: false, error: "Notes contain invisible or formatting characters (some emoji included). Type them as plain text." };
  }
  if (notes) observations.notes = notes;
  if (Object.keys(observations).length === 0) {
    return { ok: false, error: "Enter at least one observation or a note." };
  }
  return { ok: true, observations };
}

// True only when the observations on record are exactly the ones submitted: same names, same values.
export function sameObservations(
  stored: Record<string, unknown> | undefined,
  submitted: Record<string, string | number>,
): boolean {
  if (!stored) return false;
  const names = Object.keys(submitted);
  if (Object.keys(stored).length !== names.length) return false;
  return names.every((name) => Object.hasOwn(stored, name) && stored[name] === submitted[name]);
}
