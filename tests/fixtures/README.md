# Test fixtures

These JSON files are **recorded real responses**, not invented data. They exist only so tests
run offline; they must not be treated as current conditions.

| File | Source | Recorded | Notes |
| --- | --- | --- | --- |
| `open_meteo_marine_tioman_recorded_2026-10-08.json` | Open-Meteo Marine API, Pulau Tioman reference point | 2026-10-10 | Trimmed to 7 hourly values (2026-10-08T17:00Z to 23:00Z). CC BY 4.0, non-commercial. |
| `open_meteo_wind_tioman_recorded_2026-10-08.json` | Open-Meteo Forecast API (wind, 10 m), Pulau Tioman reference point, `cell_selection=sea` | 2026-10-10 | Trimmed to the same 7 hourly values as the marine fixture. CC BY 4.0, non-commercial. |
| `phn_etide_tioman_recorded_2026-10-17.json` | Pusat Hidrografi Nasional e-Tide endpoint (`etide.hydro.gov.my/functions/q_functions.php`), location `pulau_tioman` | 2026-10-17 | Trimmed to the `current_day` block (17 Oct 2026, 144 ten-minute heights, four high/low entries); the `prev_day` and `prev_1_day` blocks (18 and 19 Oct) are dropped, all kept values unmodified. Heights in metres above LAT/chart datum per the page note; time zone +08:00 is inferred. Redistribution committed on the owner's say-so (2026-10-17); not current conditions. |
| `ticon4_tioman_323a_extract_recorded_2026-10-17.csv` | TICON-4 (DGFI-TUM, SEANOE 10.17882/109129), gauge `tioman-323a-mys-uhslc_rq` | 2026-10-17 | The 50 unmodified rows for that gauge, CSV not JSON. CC BY 4.0. Units and phase convention unverified (ADR 0020). Not current conditions. |
| `data_gov_my_warning_recorded_2026-10-09.json` | data.gov.my Weather API (MET Malaysia), `limit=3` | 2026-10-09 | Unmodified. |
| `data_gov_my_warning_incl_no_advisory_recorded_2026-10-09.json` | data.gov.my Weather API, issued since 2026-10-02, `limit=100` | 2026-10-09 | Unmodified. Includes an undated "No Advisory" tropical-cyclone notice. |
| `data_gov_my_forecast_tioman_recorded_2026-10-11.json` | data.gov.my Weather API (MET Malaysia) 7-day forecast, `Rc024` Pulau Tioman, `limit=100` | 2026-10-11 | Unmodified; 7 daily rows (2026-10-11 to 2026-10-17). |
| `met_api_marine_tioman_recorded_2026-10-16.json` | MET Malaysia API v2.1 `/data`, `FORECAST`/`MARINE`, `LOCATION:499` (TIOMAN waters), today only | 2026-10-16 | Unmodified; 7 daily values. Contains no token. |
| `met_api_locations_waters_recorded_2026-10-16.json` | MET Malaysia API v2.1 `/locations?locationcategoryid=WATERS` | 2026-10-16 | Unmodified; 30 waters zones, none with coordinates. |
| `item6/item6_trace_run5_recorded_2026-10-29.json` | ADR 0025 item 6 live capture (run 5), `my-pahang-pulau-tioman`, planned 2026-10-30 03:00 UTC, 60 min; five connector payloads (data.gov.my, MET Malaysia marine, Open-Meteo marine and wind) and eight model replies | 2026-10-29 | Recorded live through the loopback model server; owner-approved fixture. Outcome `PENDING_HUMAN`, `INSUFFICIENT EVIDENCE`. Two specialist replies were rejected after return (see stage trace). Scrubbed by `tests/evaluation/item6_scrub.py`; not current conditions. |
| `item6/item6_trace_run6_recorded_2026-10-30.json` | ADR 0025 item 6 live capture (run 6, captured as `item6_run6d`), `my-pahang-pulau-tioman`, planned 2026-10-30 11:00 UTC, 60 min, `captured_at_utc` 2026-10-30 10:20:10 UTC; five connector payloads (data.gov.my, MET Malaysia marine, Open-Meteo marine and wind) and eight model replies | 2026-10-30 | Recorded live through the loopback model server; owner-approved fixture. Outcome `PENDING_HUMAN`, `INSUFFICIENT EVIDENCE` (deterministic, proposed and final). No call failed and no reply was rejected. Contains no token. Reviewed by the security and risk reviewers. Scrubbed by `tests/evaluation/item6_scrub.py`; not current conditions. See the model-reply note below. |

**Item 6 model replies are recorded input, not expected output.** The `replies` in the `item6/`
fixtures are what the model returned on one live run. Tests feed them back through the pipeline;
no test treats their wording as correct, and none should.

- **Warning applicability.** Neither code nor a model decides whether a marine warning applies to
  a site; a human must read it (`WarningNeedsHumanReadingRule`). In run 6, the
  `scenario:favourable` summary nonetheless concludes the warnings do not apply because they name
  other areas. That is a judgement the model was not asked to make, and it contradicts the same
  reply's own risk factor (applicability undetermined). Whether Pulau Tioman falls inside an area
  a warning names is not settled here, and is the human's reading in any case. At capture time
  (prompt `2026-10-17.2`) the scenario prompt had no rule against stating applicability (the
  specialist prompt had one); this reply is an example of that gap. Prompt `2026-10-30.1` adds
  the ban to the scenario and risk prompts, so these recorded replies predate it. Scenario
  text cannot relax the outcome: rules and the supervisor never read it, and the risk agent that
  does read it can only keep or tighten the result through `reconcile`.
- **`confidence`.** Each reply's `confidence` is the model's own uncalibrated estimate. It is not
  a probability, not a measure of dive safety, and never shown as confidence (AGENTS.md section 11).

Edge cases (nulls, wrong units, missing hours, truncation) are built in tests by mutating these
files in code and are labelled synthetic there.
