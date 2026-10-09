# Test fixtures

These JSON files are **recorded real responses**, not invented data. They exist only so tests
run offline; they must not be treated as current conditions.

| File | Source | Recorded | Notes |
| --- | --- | --- | --- |
| `open_meteo_marine_redang_recorded_2026-10-08.json` | Open-Meteo Marine API, Pulau Redang reference point | 2026-10-09 | Trimmed to 7 hourly values (2026-10-08T17:00Z to 23:00Z). CC BY 4.0, non-commercial. |
| `open_meteo_wind_redang_recorded_2026-10-08.json` | Open-Meteo Forecast API (wind, 10 m), Pulau Redang reference point, `cell_selection=sea` | 2026-10-09 | Trimmed to the same 7 hourly values as the marine fixture. CC BY 4.0, non-commercial. |
| `data_gov_my_warning_recorded_2026-10-09.json` | data.gov.my Weather API (MET Malaysia), `limit=3` | 2026-10-09 | Unmodified. |
| `data_gov_my_warning_incl_no_advisory_recorded_2026-10-09.json` | data.gov.my Weather API, issued since 2026-10-02, `limit=100` | 2026-10-09 | Unmodified. Includes an undated "No Advisory" tropical-cyclone notice. |

Edge cases (nulls, wrong units, missing hours, truncation) are built in tests by mutating these
files in code and are labelled synthetic there.
