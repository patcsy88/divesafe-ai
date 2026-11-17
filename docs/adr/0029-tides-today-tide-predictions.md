# ADR 0029: Tide predictions from the tides.today API

Status: PROPOSED (2026-11-17). The connector is built **off by default**
(`DIVESAFE_TIDES_TODAY_ENABLED=false`), like PHN (ADR 0021). The owner reports that written
permission from the provider (Stephen Wright, trading as Tides Today) is being sought. **Until
that wording is on file, the connector must not be switched on**, and nothing here makes the
permission verified. Reviewed by `security-reviewer` and `risk-reviewer` on 2026-11-17: no
blockers; their should-fix findings and nits are fixed, and the final round was clean.

## Context

`tides` is a required category in the interim ruleset (`api/state.py`). Its only source, PHN
e-Tide (ADR 0021), is off because PHN's wording is not on file, so the category is always missing
in production. The owner asked on 2026-11-17 to add tides.today, which offers a documented,
authenticated API with a location 3.7 km from the Pulau Tioman reference point.

The owner holds an application ID and an API key. With them, the owner made one request to each
endpoint below on 2026-11-17 from his own shell; the responses are kept outside the repository
(`~/.divesafe/tt-sample/`, mode 600). The assistant never saw the credentials.

## What is documented

- API V1 at `https://api.tidestoday.io/v1` (OpenAPI "Tides Today: Tides V1 API", version 1.1,
  "a closed API and is accessible by invite only"). HTTP Basic authentication: application ID as
  user name, API key as password. `/v1/health` needs none; the other endpoints return 401 without
  it.
- The OpenAPI document states **no units, no vertical datum, no rate limit and no meaning for
  `estimated`**. Its example heights (1.6, 5.9) fit metres but do not say so.
- The prediction-engine page (tides.today) says predictions come from the FES2014 and FES2022
  global tide models, with official national predictions used where available, and that Lowest
  Astronomical Tide is estimated from a 19-year simulation. Which applies to Teluk Tekek is not
  stated.

## Terms of service (tides.today, updated 2026-05-09)

- §2: Tides Today must not be the **sole** source for diving; official, local sources must be
  checked.
- §6 Data licence: personal, non-commercial, informational use only. Automated extraction, or use
  in another product, service, application or **safety-critical system**, needs prior written
  permission.
- §9: do not share keys publicly, do not bypass rate limits, do not remove attribution, do not
  use for emergency, navigational or safety-critical purposes.

DiveSafe is decision support with a human making the final decision (AGENTS.md sections 1 and 6),
but it is automated extraction into another application. **§6 and §9 are why the permission must
be on file first.** The request to the provider asks it to cover: diving decision support with a
human making the decision; sending the data to the hosted model; storing it in assessment
records; and how the attribution must be shown.

## What was observed (one request per endpoint, 2026-11-17)

- `GET /locations/2960/tides?dateFrom=2026-11-17T00:00:00.000Z&dateTo=2026-11-20T00:00:00.000Z`:
  `data` is a list of `{id, height, estimated, type ("low"|"high"), datetime (UTC, "Z")}`, 12
  extremes over three UTC days, alternating high and low, heights 1.10 to 2.64, `estimated` false
  on all. `meta.params` echoes the request. `location` repeats the location (below). Every
  extreme fell inside `[dateFrom, dateTo)`.
- `GET /locations/2960`: id 2960, name "Teluk Tekek", region Pahang, Malaysia, timezone
  `Asia/Kuala_Lumpur`, `lat` "2.800000", `lng` "104.133003" (strings), and a `copyright` text of
  about 1,000 characters naming FES2014/FES2022 (CNES, CLS, LEGOS via AVISO+), ETOPO 2022 (NOAA
  NCEI) and PSMSL, and saying predictions "should not be relied upon for navigation or
  safety-critical decisions".
- `GET /locations/2960/meteorology-summary` (same dates): one row per day, `date` at 00:00 UTC
  (which is 08:00 the same local day), `sunrise` and `sunset` in UTC (sunrise 22:48 UTC the day
  before, i.e. 06:48 local), and daily weather numbers. Four rows for three requested days, so
  `dateTo` is **inclusive here**, unlike the tides endpoint. Row `id` is a string (the OpenAPI
  example shows a number).
- `GET /spring-neap-tides` (same dates): four rows `{moonPhase: {name, percent}, date, spring,
  neap}`; 17 Nov was a neap day. `GET /moon-phase` returns the same `moonPhase` for one date, so
  it is not needed.
- `GET /locations/2960/tides/now`: previous and next extreme, and a `currentHeight` marked
  `estimated: true`.

## Units and datum: the basis used

- **Units: the owner's statement from the tides.today website (2026-11-17)**: tide heights in
  metres, temperature in °C, wind in km/h. The API does not state them. Items say "m, per the
  owner's reading of the tides.today website; not stated by the API".
- **Datum: not stated by the API.** The prediction-engine page suggests LAT. Items say exactly
  that and carry no depth, clearance or "water over the reef" logic (as ADR 0021 decision 8).

## Decision (proposed)

A `TidesTodayConnector` reads high and low water, sun times and spring/neap for the local days a
dive window touches.

1. **Location (option A, accepted by the owner).** A fixed, reviewed table in code:
   `my-pahang-pulau-tioman` -> location 2960 "Teluk Tekek" (2.800000, 104.133003). The item's
   `location` is the tides.today point, not the dive site. It says the point is about 3.7 km from
   the site's reference point and is a prediction for that point. No "Tioman" location exists in
   tides.today search; 2960 is the only one within 25 km.
2. **Evidence.** One item per **local day** (Asia/Kuala_Lumpur, +08:00, no daylight saving):
   category `tides`, `data_kind` PREDICTION, `source` `tides-today-prediction`, `source_version`
   `api-v1`, quality `DEGRADED`, `upstream_known=False` (the copyright names the FES models, but
   whether an official prediction or FES was used for this point is not stated, so it never
   corroborates anything, ADR 0010). `valid_at`/`valid_until` are that local day in UTC.
   Retrieval time is the only age we know.
3. **Value.** Everything sits under keys no rule or typed observation reads; never `variables`
   or `ranges`:
   - `tide`: `datum` (the wording above), `unit` "m" (with the basis), `extremes` (a list of
     `{time (ISO, +08:00), kind (high|low), height_m}` in time order), `spring` and `neap`
     (booleans as given) and `moon_phase` (`name`, and `percent` as given; its meaning is not
     stated);
   - `daylight`: `sunrise` and `sunset` (ISO, +08:00) as context only;
   - the "tide height and time only: no current speed, direction or slack-water time" statement,
     `requested_location_id` 2960, `location_name`, `spatial_scope` and `data_type`.
4. **One item, all or nothing.** Sun times and spring/neap are put inside the `tides` item, not
   as items of their own. A separate item in `tides` could make the category look present when
   the extremes failed; a separate `weather` item could do the same if `weather` ever becomes
   required. So the connector makes three calls (tides, meteorology summary, spring/neap) and any
   failure means **no item at all** and an issue: the `tides` category is missing and the result
   is INSUFFICIENT EVIDENCE.
5. **Fail closed.** It raises, and never returns partial data, when:
   - the response location id, name, latitude or longitude differs from the table, or its
     timezone is not `Asia/Kuala_Lumpur`;
   - an extreme's time is not a valid UTC timestamp, times are not strictly increasing, highs and
     lows do not alternate, a height is not a finite number in a data-sanity range, an id is
     repeated, or `estimated` is not `false` (its meaning is not documented, so an estimated
     extreme is refused, not shown);
   - the fetched extremes do not bracket each needed local day (one extreme before its start and
     one after its end), or two consecutive extremes are further apart than a plausibility bound
     (a missing high or low; the bound is a parsing check, not a safety limit, and is stated in
     code);
   - a needed day has no meteorology or spring/neap row, a row's date is not that day, sunrise is
     not before sunset or either is outside that local day, or a boolean or phase field has the
     wrong type;
   - the window starts before today (local) or ends more than a fixed horizon ahead (see 7);
   - `meta.totalPages` is not 1 (no paging is followed), a response is not JSON, is larger than
     the size cap, is nested deeper than the depth bound, or has the wrong shape.
   The request covers the needed local days plus one UTC day either side; the tides endpoint's
   `dateTo` is treated as exclusive and the other two as inclusive, as observed.
6. **No rule, no limit.** No tide threshold and no rule. `tidal_current` stays unencodable, and
   tide evidence never gives GO or CAUTION on its own. **`degraded_may_support_go` must not be
   turned on for tides** (ADR 0021 decision 4 applies unchanged).
7. **Horizon.** The API states none. Our bound: today and the next six local days. This limits
   request size and calls; it is not a statement about prediction quality.
8. **Transport.** `TIDES_TODAY_HOSTS = {"api.tidestoday.io"}`, kept **out of `ALLOWED_HOSTS`**,
   so no other connector can reach it. A getter built for this host only, https, standard port,
   no userinfo, no redirects followed, the usual size cap and deadline. The `Authorization:
   Basic` header is allowed only on a getter whose host list is exactly this host (as for MET);
   the GET getter with the shared list refuses the host. A credentialed getter must also use
   its list's header scheme (`CREDENTIAL_SCHEMES`: `METToken` for MET, `Basic` for tides.today),
   and the host lists are pinned disjoint. The credential never appears in logs, errors, issues,
   evidence or records.
9. **Credentials.** Two settings, `tides_today_app_id` and `tides_today_api_key` (`SecretStr`,
   shape-checked, no value in any message), read from secret files in production
   (`SECRET_FILES`, compose secrets with empty defaults from `docker/make-secrets.sh`). Enabled
   without both is a start-up error; both present but not enabled builds nothing.
10. **Politeness.** No published limit. Our conservative assumption, recorded as such: 6 calls a
    minute and 300 a day per process (two assessments a minute, 100 a day at three calls each),
    with the usual 60 second response cache. Concurrent cache misses are not coalesced.
11. **Attribution.** The full provider `copyright` text is kept on the item (bounded, printable)
    and stored with the record. Because the user view cuts attributions at 300 characters, the
    `attribution` field is a fixed short text naming Tides Today, FES (CNES, CLS, LEGOS / AVISO+),
    ETOPO 2022 (NOAA NCEI) and PSMSL, with a pointer to the full text. **How attribution must be
    shown is one of the questions to the provider**; its answer replaces this. The full notice
    is record-only: `agents.prompts.RECORD_ONLY_VALUE_KEYS` drops `provider_notice` from every
    prompt payload (security review 2026-11-17), so only the short `attribution` goes to the
    model host.
12. **Egress.** When enabled and the model server is not declared local, start-up logs a warning
    that the tides.today item is sent to the model host with every assessment (as for PHN). The
    item is excluded from evaluation runs to Langfuse and from the item-6 capture, as the PHN
    item is (AGENTS.md section 4 gains it). Evaluation runs exclude it by construction (no
    scenario builds the connector); `test_no_evaluation_module_builds_or_reads_tides_today` pins
    that no evaluation module names it. When the Langfuse export's fixture allow-list is built,
    it must refuse the tides.today fixture.
13. **Not included.** `tides/now` (its height is estimated and only valid for "now"), the daily
    weather numbers (one value a day is too coarse for a dive window; may be revisited), and
    `/moon-phase` (spring/neap already carries the phase).
14. **Off by default.** `DIVESAFE_TIDES_TODAY_ENABLED` defaults to false. Enabling it is the
    owner's decision after the permission wording is on file. Production compose passes it to the
    api only, as `${DIVESAFE_TIDES_TODAY_ENABLED:-false}`, and `production.env.example` sets it to
    `false` (owner, 2026-11-17); both are pinned by `tests/safety/test_production_compose.py`.

## With PHN also on

Both sources could give `tides` items. Both are DEGRADED with unknown lineage, neither
corroborates the other, and no contradiction check compares them (see `docs/risk-model.md`,
contradictory evidence). The model sees both; the engine treats both as present.

## Options not taken

- **Option B, map the dive site to its own coordinates with `/locations/nearby` at run time.**
  Rejected: a run-time lookup could silently move the point; the table is fixed and reviewed.
- **Sun times and spring/neap as separate items.** Rejected (decision 4).
- **Use `tides/now`.** Rejected (decision 13).
- **Add the host to `ALLOWED_HOSTS`.** Rejected: every default-getter connector could call it
  with no credential, and a credential could leak to other hosts.
- **Show the full copyright text in the user view.** Not done until the provider says how it
  wants attribution shown; the record keeps it whole.

## Fixture

The repository is public and §6 restricts redistribution. **The recorded responses are not
committed** unless the provider permits it. Decided by the owner (2026-11-17): tests use a
**SYNTHETIC** fixture with the observed shape, clearly labelled, with a placeholder copyright
text; the parser is also run once, locally, against the recorded responses. On 2026-11-17 it
accepted them (tides, meteorology summary, spring/neap; HTTP 200) for 18 Nov: one DEGRADED item
with four extremes and a 1023-character notice. The run trimmed the daily rows to the asked day
and skipped only the echoed-dates check, because the samples were requested for other dates;
nothing from them is committed.

## Known limits (to be carried into `docs/risk-model.md`)

- Units come from the owner's reading of the website and the datum is not stated by the API.
- A prediction for Teluk Tekek, 3.7 km from the reference point, not for any reef. Astronomical
  prediction only, with no weather or pressure effects.
- Which model (official or FES) produced the values is not stated.
- `estimated: true` extremes are refused, so a change upstream could make the category fail.
- The plausibility bound on the gap between extremes (18 h) could refuse a real long diurnal
  interval (fail closed; availability, not safety). It does **not** catch a single missing
  high/low pair: alternation still holds and the gap can stay under 18 h (a test shows a dropped
  pair in the synthetic fixture leaves a 17 h 40 min gap). Unlike PHN, there is no height series
  to cross-check against, so an incomplete list for a day can pass. The items say they list the
  provider's highs and lows, not that the list is complete.
- The call budget is per process and shared by all users; using it up makes tides missing for
  everyone (fails closed). Retrieval time may be up to the cache lifetime older than shown.
- The API version is "v1" with no change notice; a shape change makes the category fail.
- The sole-source clause (§2) is met by the human gate and by official sources the diver checks;
  the software cannot check that.

## Acceptance tests

Fixture parse; each failure in decision 5; no item at all when any of the three calls fails;
`observation_from_evidence` is `None`; the prompt carries the `tide` value and the "not a
current" wording; an engine test that this evidence alone never gives GO or CAUTION; the shared
getter refuses the host; the tides.today getter refuses other hosts, never follows a redirect,
sends Basic auth only to its own host, and the credential never appears in errors or logs;
settings and secret-file tests; the start-up egress warning; the attribution in the user view.
Mutation checks on the location check, the alternation check, the bracketing check, the horizon
and past-window checks, the `estimated` refusal, the 18 h gap bound, high above low, the time
zone, the notice length and printable checks, the single-page and echoed-dates checks, the
daily-row checks, the host separation (shared list, merged or overlapping credential lists, the
header scheme), the record-only filter for `provider_notice`, the fixed text of a validation
error, the wiring and the egress warning.
