# Data sources

**Status (2026-10-09):** connectors are implemented for Malaysia (Pulau Tioman): Open-Meteo
marine and data.gov.my warnings. Product-owner decisions: **non-commercial use only**; first
area **Pulau Tioman, Pahang** (changed from Pulau Redang, Terengganu, on 2026-10-10). Do not add endpoints, API names or sample values until a
source is verified and recorded here.

## Required categories

| `DataCategory` | Kind | Needed for |
| --- | --- | --- |
| `weather`, `wind` | Forecast + observation | Surface conditions, trend |
| `waves_swell` | Forecast + observation | Entry/exit and surface safety |
| `currents` | Forecast + observation | Drift and exertion risk |
| `tides` | Prediction (+ observed level) | Timing, slack windows |
| `sea_temperature` | Forecast + observation | Thermal protection |
| `marine_warnings` | Official notices | Hard constraints |
| `historical_observations` | Archive | Prediction, calibration |
| `site_information` | Curated knowledge | Site hazards, bathymetry |
| `local_guidance` | Curated knowledge | Regulations, best practice |

Live categories (weather through marine warnings) come from structured connectors. Curated
knowledge comes through RAG ([rag-design.md](rag-design.md)).

## Source register (Malaysia)

Verified 2026-10-09 against the providers' own documentation. "Candidate" means documented and
reachable but not yet approved or implemented. Nothing here has been called from this repo.

| Category | Provider | Official? | Access / licence | Update cadence | Resolution | Status |
| --- | --- | --- | --- | --- | --- | --- |
| `marine_warnings`, `weather` (warnings) | data.gov.my Weather API (`api.data.gov.my/weather/warning`), data from MET Malaysia | Yes (Government of Malaysia) | Keyless. Rate limit 4 requests/min. Terms of Use page not retrievable, so licence is unverified. | Warnings "when required" | Per warning (text) | **Implemented** (`divesafe.data.data_gov_my_warnings`) |
| `weather` (general) | data.gov.my Weather API (`/weather/forecast`) | Yes (MET Malaysia) | As above | Daily | State, district, town, division and "recreation centre" locations | Candidate, low value (daily text summary, Malay values) |
| `waves_swell`, `sea_temperature`, `currents` (not `tides`) | Open-Meteo Marine API (`marine-api.open-meteo.com/v1/marine`), built on DWD, MeteoFrance, ECMWF and NCEP wave/ocean models | No (aggregator of agency model output) | Keyless for **non-commercial use only**; CC BY 4.0 with required attribution to DWD and Open-Meteo; under 10,000 calls/day, 5,000/hour, 600/min | Models update every 6-24 h | About 8-25 km grid; hourly | **Implemented** (`divesafe.data.open_meteo_marine`); regional model output only, never site-level or observed |
| `tides` (official) | JUPEM tide predictions (Peninsular Malaysia, 12 locations; printed tables and the STAPS mobile app) | Yes | No API found; printed volume is paid | Annual tables | Station | **Gap**: no machine-readable source verified |
| `wind` | Open-Meteo Forecast API (`api.open-meteo.com/v1/forecast`), hourly `wind_speed_10m`, `wind_gusts_10m`, `wind_direction_10m` from the provider's automatically selected national weather model | No (aggregator of agency model output) | Keyless for **non-commercial use only**; CC BY 4.0; link "Weather data by Open-Meteo.com" required next to displayed data; under 10,000 calls/day | Model-dependent (3-hourly to 6-hourly runs) | Model-dependent (docs: 1 to 55 km) | **Implemented** (`divesafe.data.open_meteo_wind`); model forecast, never site-level or observed |
| `historical_observations` | none verified | | | | | **Gap** |
| `site_information`, `local_guidance` | none; curated corpus to be written with provenance | | | | | **Gap** (RAG corpus) |

### Implemented behaviour worth knowing

- **Site registry** (`divesafe.data.sites`): only `my-pahang-pulau-tioman`, an island-level
  reference point (2.7972, 104.166, from Open-Meteo's geocoder, GeoNames id 1734910, queried
  2026-10-10). No specific Tioman dive
  sites are registered because none has a verified coordinate source.
- **Marine connector:** all items are `is_forecast=True` and `data_type="model"`, even for hours already past, because they are model values, not observations. One `EvidenceItem` per category per UTC hour (`waves_swell`,
  `sea_surface_temperature` as `sea_temperature`, `currents`). It rejects, per category, any
  missing hour, null, unexpected unit or out-of-range value, reports it in `issues`, and
  emits nothing for that category. It does not request `sea_level_height_msl`, so `tides` can
  never be satisfied by it. The provider snaps to a grid cell (about 4.6 km from the requested
  Tioman point for marine data and 8.1 km for wind, in the recorded fixtures); the requested point, the cell and `grid_distance_km` are recorded in
  each item, with `spatial_scope` stating it is not the dive site. The window end is rounded up
  to the next hour.
- **Warnings connector:** the endpoint 301-redirects without a trailing slash, so the
  trailing-slash URL is used. Warnings are free text and multi-region; the connector reports
  every warning overlapping the dive window and never decides applicability. Timestamps have no
  documented timezone; UTC+8 is inferred from the server clock, so a warning is included if it
  overlaps the window under **either** UTC+8 or UTC. Timestamps that already carry an offset are
  rejected. Issue lookback is 30 days. Undated advisories (e.g. the "No Advisory" tropical-cyclone notice) are
  included as `advisory_without_validity` and never count as "no active warnings". A successful
  response with no overlapping dated warning and no undated advisory yields a
  `no_active_warnings` item. It means only "this feed returned no warning issued within 30 days
  that overlaps the window"; it is not a marine forecast and not proof of calm. Warning text is
  untrusted input (`untrusted_text` lists the fields).
  The rule `WarningNeedsHumanReadingRule` ([ADR 0005](adr/0005-warnings-need-human-reading.md))
  turns any warning or undated advisory into `INSUFFICIENT EVIDENCE`, so `GO` will be uncommon
  until MET Malaysia notice types are classified and verified. A page of 100 entries is refused as
  possibly truncated.
- **Failures:** connectors raise `ConnectorTransportError` or `ConnectorResponseError`; the
  evidence service converts them to `issues` and the category stays missing, so the rules engine
  returns `INSUFFICIENT EVIDENCE`.
- **HTTP hardening** (`divesafe.data.http`): https only; host allow-list on the first request
  and every redirect; at most 2 redirects; 2 MiB response cap; 30 s total deadline; error text
  omits query strings. Provider text echoed into issues is clipped and sanitised.
- **Tests** use recorded real responses in `tests/fixtures` (see its README) and no live calls.

### Wind connector behaviour (verified 2026-10-09)

- Items are `data_kind=model`, `is_forecast=True` even for past hours, graded `degraded`, one per
  UTC hour, all three variables together or nothing (any missing hour, null, wrong unit or
  out-of-range value drops the whole category and is reported).
- **Units are pinned** (`wind_speed_unit=kmh`) and checked; a different unit is rejected, never
  converted. The request prefers sea grid cells and records the cell and its distance.
- **Gusts are the maximum of the preceding hour;** speed and direction are instantaneous.
- **The producing model is unknown:** "best match" selects a model per location and the response
  does not name it. Recorded in every item's quality notes. Pinning a model would fix this but is
  a choice that needs a reason and a reviewer.
- **The direction convention is not stated** on the docs page read; do not build an exposure rule
  on "from" or "to" until verified.
- Same host family, licence and non-commercial limits as the marine API; a per-host minimum
  interval protects the quota (`api.open-meteo.com`, 0.5 s).

### Provider quota and attribution (wind and marine)

- **Combined budget:** `CachingRateLimitedGetter` counts calls to both Open-Meteo hosts together
  against the provider's documented free-tier limits (600 a minute, 5,000 an hour, 10,000 a day).
  We could not verify whether the provider counts the two hosts together, so this is deliberately
  conservative. An exhausted budget fails closed as a connector issue. Each assessment makes two
  Open-Meteo calls, so the day budget is roughly 5,000 assessments. Cache hits are free.
- **Not solved:** the budget is per process (several workers each get their own) and not per user,
  so one authenticated user can still use it up for everyone (a denial of service, not a safety
  failure). Per-user rate limits on `POST /v1/assessments`, single-flight de-duplication of
  concurrent identical calls, and quantising the window so near-identical requests share a cache
  entry are all still to do. A `planned_start` far outside the provider's forecast horizon makes
  the provider answer 400 and still spends a call.
- **Attribution:** every Open-Meteo item carries `Marine data by` or `Weather data by Open-Meteo.com
  (CC BY 4.0), https://open-meteo.com/`, and the API returns them in `attributions`. The licence
  requires a link next to displayed data, so **any client must render `attributions` beside the
  data, as a plain-text link and never as HTML**. The API returns plain strings, not structured
  `{text, url}` pairs.

### Tide source research (2026-10-09; nothing connected)

Read from each provider's own pages. **No connector exists and none should be built until the
licence question below is answered.** No key was created and no call was made to any of these.

| Candidate | What it offers | Licence and cost, as stated | Verdict |
| --- | --- | --- | --- |
| **JUPEM** (Malaysia's official tide predictions) | Tide tables for 12 Peninsular Malaysia locations; a mobile app with a free 7-day forecast | Printed volume is paid; **no API or licensed data feed found** | Best fit by authority. Needs a licence or file export from JUPEM. |
| **WorldTides** (`worldtides.info/api/v3`) | Tide heights, high/low extremes, datums, timezone, from station gauges and global models; the response names the dataset (`atlas`) and the actual point or station used (`responseLat/Lon`, `station`); `stationDistance=0` forces the global background model | API key required (**a credential you must create**); paid prepaid credits (1 credit per 7 days of heights or extremes), new accounts get free credits; copyright text must be reproduced; **see the two problems below** | **Not usable as-is.** |
| **Stormglass** | Global tide extremes and sea level, with the station name and distance reported | API key; free plan **10 requests/day, not for commercial use** (one pricing page we read says 5,000/day and commercial use, a contradiction we could not resolve); its tide data sources and safety terms were **not read** | Unverified. Read its terms and source list before any decision. |
| **Admiralty (UKHO) Tidal API** | Authoritative predictions | Covers the **British Isles and Ireland only** | Ruled out for Malaysia. |
| **Open-Meteo `sea_level_height_msl`** | Hourly model sea level including tides | Same non-commercial terms as the rest of Open-Meteo | **Not a tide prediction** (datum is global mean sea level, about 8 km model, "not suitable for coastal navigation"). Deliberately not used. |

**WorldTides: two problems, both from its own terms (updated 2026-07-12):**

1. **A safety exclusion.** The terms say you "may not use this data if anyone or anything could
   come to harm as a result of using it, including navigation or safety-critical operations", and
   "do not use WorldTides as the only source for ... life-safety decisions". DiveSafe is decision
   support for diving. Whether advice that a human can override counts as "safety-critical" is a
   legal question we cannot settle; the safe reading is that it needs **written permission from
   Brainware LLC**, which the terms invite for custom agreements.
2. **A per-user licence.** "Each API request may only be used for a single user", and the data is
   "licensed for use of individual spatial coordinates by an end user". Our response cache and any
   shared assessment would reuse one paid call for several users.

Other facts: the global background data is FES2014/FES2022 (AVISO+, satellite-derived) plus
station-derived predictions from the University of Hawaii sea-level archive. No Malaysian
authority is listed as a source, and the accuracy near Pulau Tioman is **unknown** until the
response's `atlas`, `responseLat/Lon` and station distance can be inspected with a real key.

**What none of them provides: tidal streams.** These sources give water *height* and high/low
times. The `tidal_current` risk factor needs the speed and direction of the tidal *stream*, which
is what actually moves a diver, and none of these candidates supplies it. So even with a tide
source connected, `tidal_current` stays unencodable. Tide height would support the `tides`
evidence category and timing questions only.

**Decisions needed before any connector (owner):** (a) ask JUPEM for a licence or export;
(b) if WorldTides is preferred, obtain Brainware's written permission for this use and for sharing
results across users, then register a key (kept in the environment, never in the repository);
(c) decide whether tide height alone, without tidal streams, is worth connecting.

### Known limits that affect safety use

- **data.gov.my states that marine forecast data is currently unavailable.** Warnings exist
  (the documented example is a "strong winds and rough seas" warning), but there is no official
  marine forecast through this API.
- **Open-Meteo marine values are model output, not observations.** Its own documentation says
  tides and currents are computed at about 8 km, "accuracy at coastal areas is limited", and the
  data is "not suitable for coastal navigation". Treat it as coarse regional guidance; reef-scale
  and tidal-stream conditions at a dive site are not resolved.
- **Open-Meteo's free tier forbids commercial use.** Fine for a capstone or research; a
  commercial deployment would need a paid plan or a different source.
- **No verified source of tidal predictions.** `tides` evidence cannot be satisfied from a
  verified machine-readable source yet, so any required-tides policy will correctly give
  `INSUFFICIENT EVIDENCE`.
- data.gov.my text values are in Bahasa Melayu; a connector must map them explicitly and treat
  unmapped values as invalid, not guess.

### Open questions for the product owner

1. ~~Non-commercial only?~~ **Decided: non-commercial** (Open-Meteo free tier). Attribution to
   DWD and Open-Meteo must be shown wherever its data is displayed.
2. ~~First area~~ **Decided: Pulau Tioman** (was Pulau Redang until 2026-10-10). Specific dive-site
   coordinates still need a source.
3. Is there access to official JUPEM tide data (licence or file export)? Still open.
4. ~~Wind~~ **Connected** (Open-Meteo forecast model; see "Wind connector behaviour"). Whether it is
   good enough to support a limit, and which model should be pinned, are professional decisions.

## Provenance recorded by connectors

Both connectors fill every provenance field on `EvidenceItem` (see architecture.md): marine
items are `data_kind=model`, `quality=degraded` with the grid-snap distance and a note that it is
not a site measurement; warnings are `data_kind=notice`, `quality=degraded` (usable with stated
limitations) with the validity window and the timezone-inference step. Both are graded `degraded` (usable with stated limitations); nothing
claims `validated`, because no connector checks data against an independent source, and the
engine caps any such claim on models, forecasts and notices anyway (ADR 0008). Marine items
record their `unit check` and `range check` steps; being model data they stay `degraded`.

## Connector contract

A connector in `divesafe.data` must:

1. Take a location and time window; return `EvidenceItem`s only.
2. Validate and normalize units with Pydantic; reject out-of-range or malformed values rather
   than coercing them.
3. Set `source`, `source_version`, `retrieved_at`, `valid_at`, `is_forecast`.
4. Distinguish forecast, observation and prediction. Never present one as another.
5. Fail explicitly (typed exception) on timeouts, auth errors and schema changes. Never return
   partial data as complete.
6. Read credentials from `Settings` (`SecretStr`); never log them.
7. Have contract tests against recorded, clearly labelled fixtures (no live calls in unit tests).
8. Respect provider terms, rate limits and caching rules.

## Freshness

Maximum evidence age is set via `EvidencePolicy` / `DIVESAFE_EVIDENCE_MAX_AGE_MINUTES`; it
has no built-in default and must be chosen per category with a documented reason.

### Not yet done (connectors)

- **Rate limiting and caching (done, per process):** `CachingRateLimitedGetter` caches for 60 s
  and enforces a per-host minimum interval (15 s for data.gov.my, 0.5 s for Open-Meteo). A
  blocked call fails closed. The daily Open-Meteo quota (10,000) is not tracked, and limits are
  not shared across workers.
- **Attribution display (API done):** every assessment response includes `attributions`; any UI
  must show them.
- **data.gov.my licence** is still unverified (Terms of Use page not retrievable).
- **Typed `data_kind`:** `is_forecast` currently means "model or forecast output"; observations,
  predictions and notices are not yet distinguished by a typed field on `EvidenceItem`.
- **Real fetch time:** `retrieved_at` is the caller-supplied `now`; the orchestrator must pass
  the actual fetch time.
