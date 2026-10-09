# ADR 0010: Source lineage and canonical agent input

- Status: Accepted
- Date: 2026-10-10

## Context

A proposal for a multi-source data stack suggested comparing Open-Meteo Marine with Copernicus
Marine and raising confidence when they agree. Open-Meteo's own marine data-source table (read
2026-10-09) lists Copernicus Marine's global physics analysis and forecast
(`GLOBAL_ANALYSISFORECAST_PHY_001_024`) as the source of its currents and sea-surface temperature,
so the two would agree **by construction** and the agreement would carry no information. Wave
values come from whichever of several wave models the provider selects and the response does not
say which. Separately, agents read provider-shaped payloads (`wave_height`, `ocean_current_velocity`,
`wind_speed_10m`), so replacing a provider would change what agents see.

## Decision

**Lineage on evidence.** `EvidenceItem` gains `upstream` (the upstream products it derives from, as
the provider states them) and `upstream_known` (true only when that list is complete; it then must
name at least one). The default is unknown. `domain.are_independent(a, b)` is true only if **both**
are known and the lists share nothing; `can_corroborate` also needs the same category. Unknown
lineage never corroborates, because unknown does not mean different.

**What the connectors claim.** Marine currents and sea-surface temperature: the Copernicus physics
product (known). Marine waves and wind: unknown (the producing model is not reported). Warnings:
`met-malaysia:warnings` (known). Each connector's quality notes say so.

**Canonical agent input.** Agents read marine and wind evidence as canonical, unit-bearing
conditions (`wave_height_m`, `current_speed_kmh`, `wind_speed_kmh`, ...) parsed by
`observation_from_evidence`, plus the lineage, the engine's judged quality (not the connector's
claim) and the connector's limitations. Provider variable names and the raw value are not sent.
A value that fails validation, or is in an unexpected unit (never converted), sends no provider
data at all. Free-text notices keep their raw text, inside the existing untrusted block. Wind is
now a typed observation. The system prompt gains a rule that agreement counts as corroboration
only between independent items (prompt version `2026-10-10.1`).

## Consequences

- "Open-Meteo and Copernicus agree" can never be presented as confirmation for currents or
  temperature. A direct Copernicus adapter would add history, depth levels and provenance, not an
  independent check on those two values.
- Source disagreement and corroboration scoring (not built) must use `can_corroborate`. Its
  tolerance per factor is a number that must come from the dive professional, not from code.
- Replacing a provider no longer changes what agents see, provided the new connector maps to the
  same canonical fields.

## Not solved

- Lineage is only as good as what providers state. Open-Meteo's table could change; the claim is
  dated in this ADR and in `docs/data-sources.md`.
- Several upstream models that all derive from one numerical weather model would need their lineage
  named by hand; nothing detects hidden shared ancestry.
- Contradictory evidence is still not detected by the engine (see `docs/risk-model.md`).
