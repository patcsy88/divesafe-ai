# ADR 0020: Tide predictions from TICON-4 tidal constants

Status: PROPOSED (2026-10-17). Nothing is built. It reverses the owner's 2026-10-10 decision to skip tide height, so it needs the owner's acceptance first. It needs `risk-reviewer` and `security-reviewer` before any code, because it adds a data source, a dependency and a new evidence category value.

## Context

`tides` is a required category in the interim ruleset (`api/state.py`) and has no source, so every assessment is INSUFFICIENT EVIDENCE for that reason as well as for the missing signed limits (`docs/data-sources.md`, "Tide source research"). JUPEM has no usable feed, WorldTides is refused by its own terms for life-safety use, and Stormglass is unread.

tide.is shows tide times for Tioman. It has no API and no terms we could find, so we will not read its pages. Its footer says it computes predictions from TICON-4 constants. That dataset is published openly, so the same route is open to us directly.

## What was checked (2026-10-17)

- **Dataset:** TICON-4, Hart-Davis, Dettmering and Seitz (2025), DGFI-TUM, SEANOE, DOI 10.17882/109129. Licence **CC BY 4.0** (the landing page names it): attribution is required, commercial use is allowed. 50 constituents for 4,383 gauges per the landing page. The CSV we downloaded has 241,900 rows and 4,838 gauge names (the landing page says 4,383; not reconciled).
- **A Tioman gauge exists:** `tioman-323a-mys-uhslc_rq`, 2.807°N 104.14°E, about 3 km from our reference point (2.7972, 104.166). Source UHSLC research quality. Record 13/11/1985 to 11/03/2020, 36 years, 285,475 observations, 0.0 missing, "No obvious issues", type Coastal. 50 constituents, M2 59.33, S2 18.46, K1 45.92, O1 34.24 (values as stored; unit unconfirmed, see below).
- **Form of the tide:** (K1+O1)/(M2+S2) = 1.03, which is the mixed, mainly diurnal range of the usual classification. Heights and timing will therefore differ in character from a purely semi-diurnal coast, and a "high water at the same time each day" assumption would be wrong.
- **Other Malaysian gauges in the file:** Sedili (96 km), Kuantan (154 km), Johor Bahru, Kukup, Keling, Cendering, Kelang, Lumut, Geting, Penang, Langkawi, Bintulu, Miri, Kota Kinabalu, Tawau, Sandakan. Only Tioman is near the first site.
- **Fixture:** `tests/fixtures/ticon4_tioman_323a_extract_recorded_2026-10-17.csv`, the 50 unmodified rows for that gauge.

## Not established (must be closed before a connector)

1. **Units and phase convention.** The TICON-4 manual (`.../data/122852.pdf`) could not be read by my tools. The TICON-3 manual says amplitude in cm and phase as a Greenwich phase lag, range 0 to 360. TICON-4's file has phases from -180 to 180, so the convention may differ. Amplitude units, the sign of the lag, the time zone (UTC assumed) and the treatment of nodal corrections are **unverified**. Someone must read the TICON-4 manual and record the answer here.
2. **Datum.** The gauge's `datum_information` is "Unspecified". The constants give the tidal variation about the record's mean level. They do **not** give height above chart datum, lowest astronomical tide, or any level a diver or chart uses. tide.is takes its datum heights from a separate dataset (Hart-Davis, Sulzbach and Talke, SEANOE 10.17882/111620), which we have not read or licensed-checked. Until a datum is established, predictions may only be used as **relative** levels (change from mean), never as depth.
3. **Accuracy at the dive site.** One harbour gauge cannot say what the tide does at a reef. Local shifts in timing and range are unknown. The reference gauge is about 3 km from the reference point.
4. **A validation source.** No check yet that our predicted times match any independent prediction (JUPEM's tables, an almanac, tide.is read by a person for a spot-check only).
5. **The prediction method.** See Options.

## Options

1. **Do nothing.** `tides` stays unevidenced. Honest, but assessments can never be complete.
2. **Own harmonic synthesis.** Write the sum of cosines with nodal corrections and astronomical arguments. No dependency, but 50 constituents each need Doodson numbers, speeds, nodal factors and equilibrium arguments. Those are numeric constants from the literature; copying them from memory is the failure the project forbids. Only acceptable if every constant is taken from a cited table and tested.
3. **Use an established harmonic-prediction library.** Smaller code, but a new dependency, which needs this ADR's acceptance and a licence, maintenance and supply-chain check. **No library has been evaluated here**; naming one would be a guess.
4. **Fetch tide.is pages.** Rejected. No API and no terms, and the site is a small unofficial service.

## Decision proposed

Option 3 if a library passes review, otherwise option 2 with cited constants, over option 1 only if the owner wants `tides` evidence. In either case:

- **Offline and reproducible.** The constants live in the repository as the labelled fixture file; no runtime download. The connector computes from them for a given window and fixed clock.
- **Evidence shape.** Category `tides`, `data_kind` PREDICTION (never observation), `source` `ticon4-harmonic`, quality `DEGRADED` until the open items above are closed. Lineage `upstream` records TICON-4 and GESLA-4 with `upstream_known=True`. Quality notes state: astronomical tide only (no weather or pressure effects), one harbour gauge, datum unspecified, gauge record ended 2020.
- **Values.** Relative level change and the times of local highs and lows within the window, in UTC with the +08:00 date stated. No absolute height until a datum is set.
- **Attribution.** CC BY 4.0 text and the DOI are carried on every item and rendered by clients like the Open-Meteo attribution.
- **Fails closed.** A window outside the supported dates, a missing gauge or a constituent set that fails validation produces a connector issue and no item.
- **No threshold and no rule.** This ADR adds no numeric limit. A tide height rule needs a dive professional's signed limit (the questionnaire's tide item), exactly like wind and waves. It does not supply tidal **streams**, which is what moves a diver; `tidal_current` stays unencodable.

## Consequences

- `tides` could be satisfied with a degraded prediction. The interim ruleset still returns INSUFFICIENT EVIDENCE because no limits are signed, so the visible outcome does not change.
- The `tide_current` specialist would read a tide prediction beside coarse model currents. It must not treat the one as the other.
- A prediction with an unverified phase convention could be wrong by hours. This is the main risk and the reason the validation test is a precondition, not a follow-up.
- A computed prediction can look more authoritative than a model forecast. The wording on items and in the UI must say predicted, astronomical, harbour gauge.

## Acceptance tests required before this becomes Accepted

- The TICON-4 manual read and the units, sign, range and time zone recorded in this ADR.
- Predicted high and low times for Tioman compared with an independent source for at least one week, with the largest time and height differences written down. Set the tolerance with the owner or dive professional; this ADR sets none.
- Failure tests: wrong constituent name, duplicate constituent, non-finite number, out-of-range date, missing gauge.
- Mutation checks on the phase sign and the nodal correction.
- Safety tests that a `tides` item alone never yields GO or CAUTION, and that the item is never shown as an observation.
- Reviews by `risk-reviewer` and `security-reviewer` (new data source, new dependency or hand-written constants).

## Decisions for the owner

1. Reverse the 2026-10-10 skip and pursue tide predictions at all?
2. Library (needs an evaluation) or hand-written synthesis with cited constants?
3. Is a relative, degraded, harbour-gauge prediction worth building before the dive professional says whether tide timing matters (questionnaire tide item)?
