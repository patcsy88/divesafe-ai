# Data sources

**Forecast connector (2026-10-11).** `divesafe.data.data_gov_my_forecast` reads MET Malaysia's
7-day general forecast from `api.data.gov.my/weather/forecast/` for Pulau Tioman (`Rc024`, a
recreation-centre location). The API docs say marine forecast data is unavailable, so this is a
land-style rain, thunderstorm and haze forecast plus daily minimum and maximum temperature. It has
no wind, wave, swell, current or tide. Evidence: category `weather`, `forecast`, `degraded`,
upstream unknown, one item per Malaysia-time day in the window; a window the forecast does not
fully cover is rejected. Bahasa Melayu phrases are matched to a reviewed table and unmatched ones
are kept as received. `weather` is not a required category, so this item changes no outcome. The
Weather API allows 4 requests per minute across all its endpoints; the getter now enforces that as
a shared budget (it replaces the 15-second spacing) and fails closed when it is used up. Fixture:
`data_gov_my_forecast_tioman_recorded_2026-10-11.json`.

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
| `weather` (general) | data.gov.my Weather API (`/weather/forecast`) | Yes (MET Malaysia) | As above | Daily | State, district, town, division and "recreation centre" locations | **Implemented** (`divesafe.data.data_gov_my_forecast`); island-level, no marine values, not required by any policy |
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

### Further source research (2026-10-10; nothing connected)

Prompted by a proposed multi-source stack. Read from the providers' own pages; no key created, no
call made.

- **Open-Meteo Marine and Copernicus Marine are not independent.** Open-Meteo's data-source table
  lists the Copernicus physics product as its source of currents and sea-surface temperature (and
  a Copernicus wave model among several wave sources). Their agreement would carry no information.
  Recorded as lineage on every item (ADR 0010).
- **Copernicus Marine** (physics product `GLOBAL_ANALYSISFORECAST_PHY_001_024`): 1/12 degree (about
  9 km), hourly surface fields, updated daily at 08:00 UTC, 10-day forecast, a two-year sliding
  history, NetCDF. Includes a surface-current dataset with wave and tidal drift (SMOC). Needs a
  **registered account** to subset and download. Licence: free, worldwide, permits commercial use
  and derived products, **requires attribution** ("Generated using E.U. Copernicus Marine Service
  Information" with the product DOIs). Its value to DiveSafe is history for a site baseline, depth
  levels and authoritative provenance, not an independent check on Open-Meteo's currents.
  Adapter dependencies (`copernicusmarine`, `xarray`, `netCDF4`, `numpy`, `pandas`) need an ADR.
- **MET Malaysia API** (`api.met.gov.my`, v2.1): **token required** (free registration);
  documented limits 1,000 requests a day and 3 a minute. Datasets: general forecast, **marine
  forecast by named waters** (for example Terengganu Waters), and warnings in five categories
  including strong wind and rough seas. A marine forecast is something `data.gov.my` lacks. Our
  source for this is a 2022 community copy of the documentation and the API home page, so
  **current behaviour is unverified until a token is used**; the marine forecast needs a start
  date of today or later (no history). MET also states its open data may be reused for any
  purpose.

  **Findings 2026-10-10 (nothing connected; no token yet):**
  - The MET website marine page (`met.gov.my/en/forecast/marine/waters/<code>/`) shows a 7-day
    forecast by state waters (weather text, wind direction and speed range, wave range with no unit
    shown). It is HTML only, with no issue time, model or lineage. Its **copyright notice forbids
    copying, reproducing, publishing or commercially dealing with the site's contents without prior
    written consent**, so **do not scrape it**. `robots.txt` returns 404. A link to it for human
    readers is fine; ingesting its values is not.
  - The MET **Open Data page** says all open datasets on `api.met.gov.my` (introduced 2014) are
    accessible for integration and reuse by the public, government agencies and private entities. It
    names no licence; record the exact terms once registered. The Government of Malaysia accepts no
    liability for loss from the data.
  - The API home page offers "general weather forecast data" and says to contact MET for anything
    else. It lists 1,000 requests a day and a burst of 3 a minute; a 2022 community copy says 2,000 a
    day and 10 a minute. **Plan for the stricter limit.** Token header: `Authorization: METToken
    <token>`. v1 is stated as deprecated.
  - The 2022 copy lists marine forecast (`/data` for locations in the WATER category; wind speed as a
    range with a unit, such as "20-30 km/h", code `FMWS`) and warnings. **Whether marine data is
    still offered is unconfirmed** and the current docs are behind login. data.gov.my still states
    marine forecast is unavailable.
  - **Unknown until a token is used:** datatype codes and units for wave height, whether waves are a
    range, issue time, the model behind it, the `/locations` id covering Pulau Tioman (the area is a
    state-wide waters zone, not a dive site), and the current base URL.
  - **Next:** the user registers with their own name and email and sets `DIVESAFE_MET_API_TOKEN`
    (environment only). Then read `/datatypes` and `/locations`, record a real fixture, and build a
    connector (https, host allow-list, typed errors, quota guard, `degraded` quality). A second
    source cannot count as corroboration until its lineage is known (ADR 0010) and the dive
    professional gives a per-factor tolerance.
  **Findings 2026-10-16 (token used; developer guide read while logged in; nothing connected):**
  - **Base URL is `https://api.met.gov.my/v2.1/`**; `/v2/` is deprecated. Header
    `Authorization: METToken <token>`. The guide states 10 requests a minute and 2,000 a day; the
    public page says 3 and 1,000. **Plan for the stricter pair.** Responses carry `ETag` and
    `Cache-Control: max-age=60`. No licence text is given, only a liability disclaimer.
  - Marine forecast and warnings are included at registration. `/locations` needs
    `locationcategoryid` (the category is `WATERS`, not `WATER`); without it, or with the wrong
    value, it returned 403 "You do not have permission". `/datatypes` pages overlap by one entry.
  - Waters zones are areas with no coordinates. **`LOCATION:499` is `TIOMAN`.** Neighbouring zones:
    `502` Pahang, `501` Terengganu, `666` East Johor. How far zone 499 extends is not stated.
  - A real marine response for `LOCATION:499` (fixture `met_api_marine_tioman_recorded_2026-10-16`):
    daily values only. `FMWS` wind speed is a range string (`"10-20"`, unit `km/h`), `FMWH` wave
    height a range string (`"0.5 - 1.0"`, unit `m`), `FMWD` wind direction a code (`"VRB"`, no
    convention stated), plus weather text for morning, afternoon and night. **No issue time, no
    model, no validity window** (`valid_from` and `valid_to` are null). The date is a calendar day
    at +08:00. Wave height does not say which statistic it is (significant height or maximum).
  - `start_date` must be today or later: no history.
- **Not verified:** GEBCO, EMODnet, Meteomatics.
- **Open-Meteo `visibility` is atmospheric**, not underwater: its docs describe viewing distance
  influenced by cloud, humidity and aerosols. It must never stand in for dive visibility.
- **MET's published warning criteria are not diving limits.** The warning text quotes figures for
  strong wind and rough seas; they are an official hazard signal (handled by
  `WarningNeedsHumanReadingRule`), not thresholds to encode.

- **Lineage claims to verify:** Open-Meteo's marine data-source table (docs page
  `open-meteo.com/en/docs/marine-weather-api`, read 2026-10-09) is the only basis for recording
  currents and sea-surface temperature as the Copernicus physics product; a domain reviewer has
  not confirmed it. Warnings name `met-malaysia:warnings` but are **not** claimed complete.

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

**JUPEM check (2026-10-11, incomplete).** A search shows JUPEM's STAPS page and app (a table of
tide forecasts for Malaysian stations, seven days free, per the Play Store listing) and a JUPEM
product page listing paid tidal data (MYR 5.00 per month per station) and printed tables. The
search snippet for the station list shows Pulau Tioman among the stations. From my tools
`jupem.gov.my` returned transport errors on every page, so I could not read the terms or look for
a documented machine-readable feed. The app's reviews say it often shows no data. Treat as
**unverified**: no API, no licence for automated reuse. Next step is a written request to JUPEM
(owner), or manual attributed entry of published values. Do not scrape.

**JUPEM second pass (2026-10-11).** Still no JUPEM page could be read: `www.jupem.gov.my` and
`jupem.gov.my` (home, the price page, a news page on STAPS, the public page) all failed with
transport errors or timed out from my tools, over https and http. Established from other pages:
the app store listings for STAPS (publisher: Jabatan Digital Negara; copyright: JUPEM) say it shows
seven consecutive days of tide forecast tables, derived from JUPEM tide gauge stations, free of
charge, for a person using the app. Ratings are 1.4 of 5 (Apple) with reviews saying it shows no
data. Neither listing mentions an API, a download or terms for reuse. A search of data.gov.my's
API documentation found Data Catalogue, OpenDOSM, transport and Weather APIs, and no tide API;
I did not find a JUPEM tide dataset there (the search itself returned little, so this is
"not found", not "does not exist"). **Still unverified:** a documented feed, any licence for
automated reuse, and whether Pulau Tioman is in the station list (a search snippet only).
Free personal viewing in an app is not permission for DiveSafe to fetch or republish the values.
**Route:** (1) the owner asks JUPEM in writing (the support email on the Play listing is
roziana.ramlee@jupem.gov.my; confirm the right contact on JUPEM's own site first), or files a
request at data.gov.my/data-request; (2) until answered, nothing is built.

**Decided 2026-10-10 (owner): skip tide height for now.** No tide connector is built and no tide
credential or licence is pursued. `tides` and `tidal_current` stay unevidenced, so any policy that
requires them gives `INSUFFICIENT EVIDENCE`. Revisit if the dive professional says tide timing
matters for Tioman, or if JUPEM data becomes available.

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
3. ~~Official JUPEM tide data?~~ **Decided: skip tide height for now** (2026-10-10); not pursued.
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

- **Known gaps from the forecast connector review (2026-10-11):**
  - No single-flight: concurrent assessments all miss the cache before the first call returns, so
    a burst can use the 4-a-minute data.gov.my budget and make the warnings call fail. That ends as
    `INSUFFICIENT EVIDENCE` (safe), but one user could do it to everyone. The forecast is capped at
    1 of the 4 calls, which helps only partly.
  - The warnings request's `timestamp_start` is the window start minus 30 days to the second, so it
    is almost never a cache hit. Rounding it down to the day would allow caching.
  - The weather specialist can receive warnings, wind and forecast items together; the agent cap is
    120 items (`agents/runner.py`). A dive window of up to 24 hours adds at most 2 forecast items. If
    the cap is exceeded the specialist's finding is dropped (an `AgentError`, recorded as an agent
    issue), not the assessment.
  - Forecast freshness is judged from our retrieval time, not the provider's issue time, and the
    `date` is read as a UTC+8 day without the both-zones hedge the warnings use. Harmless while
    `weather` is not a required category; fix both before it ever is.
  - The data.gov.my licence and attribution are unverified, and no attribution is emitted for
    either data.gov.my connector.

- **Rate limiting and caching (done, per process):** `CachingRateLimitedGetter` caches for 60 s
  and enforces a per-host minimum interval (0.5 s for Open-Meteo) and call budgets: 4 a minute for
  data.gov.my (of which the forecast may use 1, so it cannot starve the warnings), and Open-Meteo's
  600 a minute, 5,000 an hour and 10,000 a day. A blocked call fails closed. Limits are per process
  and not shared across workers.
- **Attribution display (API done):** every assessment response includes `attributions`; any UI
  must show them.
- **data.gov.my licence** is still unverified (Terms of Use page not retrievable).
- **Typed `data_kind`:** `is_forecast` currently means "model or forecast output"; observations,
  predictions and notices are not yet distinguished by a typed field on `EvidenceItem`.
- **Real fetch time:** `retrieved_at` is the caller-supplied `now`; the orchestrator must pass
  the actual fetch time.
