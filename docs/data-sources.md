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

## Source register (fill before building a connector)

| Category | Provider | Official? | Access / licence | Update cadence | Spatial resolution | Verified by / date |
| --- | --- | --- | --- | --- | --- | --- |
| _none yet_ | | | | | | |

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
