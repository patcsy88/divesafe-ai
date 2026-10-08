# Data sources

**Status:** no connector is implemented and no external provider has been selected or
verified. Do not add endpoints, API names or sample values until a source is verified and
recorded here.

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
| `marine_warnings`, `weather` (warnings) | data.gov.my Weather API (`api.data.gov.my/weather/warning`), data from MET Malaysia | Yes (Government of Malaysia) | Keyless. Rate limit 4 requests/min. Terms of Use page not retrievable, so licence is unverified. | Warnings "when required" | Per warning (text) | Candidate |
| `weather` (general) | data.gov.my Weather API (`/weather/forecast`) | Yes (MET Malaysia) | As above | Daily | State, district, town, division and "recreation centre" locations | Candidate, low value (daily text summary, Malay values) |
| `waves_swell`, `sea_temperature`, `currents`, `tides` (sea level) | Open-Meteo Marine API (`marine-api.open-meteo.com/v1/marine`), built on DWD, MeteoFrance, ECMWF and NCEP wave/ocean models | No (aggregator of agency model output) | Keyless for **non-commercial use only**; CC BY 4.0 with required attribution to DWD and Open-Meteo; under 10,000 calls/day, 5,000/hour, 600/min | Models update every 6-24 h | About 8-25 km grid; hourly | Candidate (model forecast, not observation) |
| `tides` (official) | JUPEM tide predictions (Peninsular Malaysia, 12 locations; printed tables and the STAPS mobile app) | Yes | No API found; printed volume is paid | Annual tables | Station | **Gap**: no machine-readable source verified |
| `wind` | Not covered by the marine API. Would come from Open-Meteo's separate forecast API (not yet reviewed) or MET Malaysia | | | | | **Gap** |
| `historical_observations` | none verified | | | | | **Gap** |
| `site_information`, `local_guidance` | none; curated corpus to be written with provenance | | | | | **Gap** (RAG corpus) |

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

1. Is non-commercial-only data acceptable (capstone scope), or is commercial use planned?
2. Which Malaysian dive area first (the choice fixes coordinates, relevant warnings and the
   RAG corpus)?
3. Is there access to official JUPEM tide data (licence or file export)?

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
