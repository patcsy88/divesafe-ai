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

Edge cases (nulls, wrong units, missing hours, truncation) are built in tests by mutating these
files in code and are labelled synthetic there.
