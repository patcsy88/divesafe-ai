# Data sources

**Status (2026-10-09):** two connectors are implemented for Malaysia (Pulau Redang): Open-Meteo
marine and data.gov.my warnings. Product-owner decisions: **non-commercial use only**; first
area **Pulau Redang, Terengganu**. Do not add endpoints, API names or sample values until a
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
| `wind` | Not covered by the marine API. Would come from Open-Meteo's separate forecast API (not yet reviewed) or MET Malaysia | | | | | **Gap** |
| `historical_observations` | none verified | | | | | **Gap** |
| `site_information`, `local_guidance` | none; curated corpus to be written with provenance | | | | | **Gap** (RAG corpus) |

### Implemented behaviour worth knowing

- **Site registry** (`divesafe.data.sites`): only `my-terengganu-pulau-redang`, an island-level
  reference point (5.77736, 103.00759, from Open-Meteo's geocoder). No specific Redang dive
  sites are registered because none has a verified coordinate source.
- **Marine connector:** all items are `is_forecast=True` and `data_type="model"`, even for hours already past, because they are model values, not observations. One `EvidenceItem` per category per UTC hour (`waves_swell`,
  `sea_surface_temperature` as `sea_temperature`, `currents`). It rejects, per category, any
  missing hour, null, unexpected unit or out-of-range value, reports it in `issues`, and
  emits nothing for that category. It does not request `sea_level_height_msl`, so `tides` can
  never be satisfied by it. The provider snaps to a grid cell (about 4 km from the requested
  Redang point in testing); the requested point, the cell and `grid_distance_km` are recorded in
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
2. ~~First area~~ **Decided: Pulau Redang.** Specific dive-site coordinates still need a source.
3. Is there access to official JUPEM tide data (licence or file export)? Still open.
4. Wind is still a gap (not in the marine API).

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
