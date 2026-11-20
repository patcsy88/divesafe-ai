# ADR 0029: Tide predictions from the tides.today API

Status: PROPOSED (2026-11-17). **Written permission on file (2026-11-18); the connector must still
not be switched on (see Switch-on conditions below).** Stephen (Ste) Wright, trading as Tides Today,
replied on 2026-11-18 to the owner's requests of 2026-11-17. The reply came from the provider's
personal domain, not the tides.today address of earlier mails in the same thread, with the same
signature; the owner accepted it as the provider (2026-11-19), and the identity was not otherwise
checked. The reply: "Feel free to use the API key as you see fit, you have my full permission to use
it as you describe." The use described: V1 tide, moon and meteorology data in a recreational-diving
decision-support tool where a qualified human makes the final decision and the data is never the
sole source (that the human is qualified is a condition of the deployment, through who is given a
DiveSafe API key, and is not checked by the software); storing it in assessment records; sending it
to a hosted model that writes the explanation for the human reviewer; and the provider's copyright
notice shown with every item. The owner keeps the full thread, with headers, outside the repository.

Scope, as read by the owner (2026-11-19): the reply covers API V2 (Amendment 1) with the same key.
It covers the owner's own deployment with the owner's key, not anyone else running this code, and
non-commercial use only; a commercial deployment must ask again. It does not cover evaluation
exports to Langfuse, the item-6 capture, or committing recorded responses to this repository; those
exclusions (decision 12, Fixture) stay. It does not lift §2 (never the sole source) or §9 (no key
sharing, no bypassing rate limits, keep attribution), and it does not make the data validated or
safe. The hosted model does more than write the explanation: the item is in the Reason-stage prompts
that read tides, so the model also interprets it in specialist findings, the three scenarios and a
risk proposal that `reconcile` can only tighten. The owner reads this as within the decision support
described (2026-11-19). §9 also forbids emergency, navigational or safety-critical use, and the
provider's own notice says the predictions should not be relied on for navigation or safety-critical
decisions. The owner reads the permission as covering decision support as described: a human
decides, the data is never the sole source, and DiveSafe is not a safety authority. That notice
still applies, and every item's quality note keeps stating the terms. The reply did not answer the
questions on units and vertical datum, meteorology units, rate limit and caching, or spring/neap
access (403), so Known limits and Amendment 1 are unchanged and the call budget is still our
assumption.

**Switch-on conditions.** The connector stays built **off by default**
(`DIVESAFE_TIDES_TODAY_ENABLED=false`). It may be switched on only after (1) the full copyright
notice is shown once with every assessment view that holds a tides.today item (evaluated or
withheld), deduplicated (decision 11; owner decision, 2026-11-19) and (2) a separate owner decision
to switch it on, and condition (1) also needs the owner to have answered the OPEN QUESTION in
decision 11. State: (1) built, pending security and risk review, not yet rendered in a real
browser (one manual render is needed before condition (1) counts as met), and the OPEN QUESTION
in decision 11 (whether once per assessment meets the provider's "with every item") is not yet
answered; (2) the owner's decision to switch it on, not taken. The provider's mail of 2026-11-17
says the key is deactivated if it is not used for three months; a deactivated key makes the
tides.today item fail (tides missing, so INSUFFICIENT EVIDENCE, when it is the only tide source),
an availability issue, not a safety one.

Reviewed by `security-reviewer` and `risk-reviewer` on 2026-11-17: no blockers; their should-fix
findings and nits are fixed, and the final round was clean.
The permission text (2026-11-19) was reviewed by `security-reviewer` and `risk-reviewer`.
Amended 2026-11-17 (Amendment 1, owner): the connector now uses API V2 with an `x-api-key` header;
the application ID and spring/neap are removed. See Amendment 1 at the end. Amendment 1 was reviewed
by `security-reviewer` and `risk-reviewer` (three rounds, 2026-11-18); no blockers remain.
Amended 2026-11-20 (Amendment 2, owner): the dive site `my-pahang-tiger-reef` is mapped to the same
Teluk Tekek point as a proxy, 13.4 km from the Tiger Reef dive site (position as published by PADI). The written permission (on file since 2026-11-18) is
read by the owner as covering it; see Amendment 2 at the end.

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

## Units and datum: the basis used (V1; superseded by Amendment 1, decisions 3 and 5)

- **Units: the owner's statement from the tides.today website (2026-11-17)**: tide heights in
  metres, temperature in °C, wind in km/h. The API does not state them. Items say "m, per the
  owner's reading of the tides.today website; not stated by the API".
- **Datum: not stated by the API.** The prediction-engine page suggests LAT. Items say exactly
  that and carry no depth, clearance or "water over the reef" logic (as ADR 0021 decision 8).

## Decision (proposed)

Decisions 2, 3, 4, 5, 8, 9 and 13 below describe V1. Where Amendment 1 differs, it governs; each
affected decision is marked.

A `TidesTodayConnector` reads high and low water, sun times and spring/neap for the local days a
dive window touches.

1. **Location (option A, accepted by the owner).** A fixed, reviewed table in code:
   `my-pahang-pulau-tioman` -> location 2960 "Teluk Tekek" (2.800000, 104.133003). The item's
   `location` is the tides.today point, not the dive site. It says the point is about 3.7 km from
   the site's reference point and is a prediction for that point. (Amendment 2 adds
   `my-pahang-tiger-reef` -> the same location 2960, "about 13.4 km from the Tiger Reef dive site
   (position as published by PADI)"; the distance is stated per site, and no search was made around
   the reef: see Amendment 2.) No "Tioman" location exists in
   tides.today search; 2960 is the only one within 25 km.
2. **Evidence.** One item per **local day** (Asia/Kuala_Lumpur, +08:00, no daylight saving):
   category `tides`, `data_kind` PREDICTION, `source` `tides-today-prediction`, `source_version`
   `api-v1`, quality `DEGRADED`, `upstream_known=False` (the copyright names the FES models, but
   whether an official prediction or FES was used for this point is not stated, so it never
   corroborates anything, ADR 0010). `valid_at`/`valid_until` are that local day in UTC.
   Retrieval time is the only age we know.
   *(Superseded by Amendment 1: `api-v2`; lineage per Amendment 1 decision 4.)*
3. **Value.** Everything sits under keys no rule or typed observation reads; never `variables`
   or `ranges`:
   - `tide`: `datum` (the wording above), `unit` "m" (with the basis), `extremes` (a list of
     `{time (ISO, +08:00), kind (high|low), height_m}` in time order), `spring` and `neap`
     (booleans as given) and `moon_phase` (`name`, and `percent` as given; its meaning is not
     stated);
   - `daylight`: `sunrise` and `sunset` (ISO, +08:00) as context only;
   - the "tide height and time only: no current speed, direction or slack-water time" statement,
     `requested_location_id` 2960, `location_name`, `spatial_scope` and `data_type`.
   *(Superseded by Amendment 1: no `spring`/`neap`; `moon_phase` is `{name, fraction}`.)*
4. **One item, all or nothing.** Sun times and spring/neap are put inside the `tides` item, not
   as items of their own. A separate item in `tides` could make the category look present when
   the extremes failed; a separate `weather` item could do the same if `weather` ever becomes
   required. So the connector makes three calls (tides, meteorology summary, spring/neap) and any
   failure means **no item at all** and an issue: the `tides` category is missing and the result
   is INSUFFICIENT EVIDENCE.
   *(Superseded by Amendment 1: the three calls are location, tide extremes and ephemeris.)*
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
   *(Superseded by Amendment 1: this list adapted to V2, plus decisions 3, 4 and 6 there; both
   date filters are inclusive.)*
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
   *(Superseded by Amendment 1: the header is `x-api-key`, never `Authorization` (decision 1).)*
9. **Credentials.** Two settings, `tides_today_app_id` and `tides_today_api_key` (`SecretStr`,
   shape-checked, no value in any message), read from secret files in production
   (`SECRET_FILES`, compose secrets with empty defaults from `docker/make-secrets.sh`). Enabled
   without both is a start-up error; both present but not enabled builds nothing.
   *(Superseded by Amendment 1: one setting, `tides_today_api_key`.)*
10. **Politeness.** No published limit. Our conservative assumption, recorded as such: 6 calls a
    minute and 300 a day per process (two assessments a minute, 100 a day at three calls each),
    with the usual 60 second response cache. Concurrent cache misses are not coalesced.
11. **Attribution.** The full provider `copyright` text is kept on the item (bounded to
    `MAX_NOTICE_CHARS` = 2000, printable) and stored with the record. The user view shows it
    whole, shown once with every assessment view that holds a tides.today item (evaluated or
    withheld), deduplicated (owner decision, 2026-11-19): `AssessmentView.provider_notices`, built
    from evidence and withheld evidence whose source is `tides-today-prediction`, whose category
    is `tides` and whose `provider_notice` value is a non-empty, printable string (the connector's
    own rule: ordinary newlines are not allowed by it, so a notice that is not printable is not
    shown and the missing flag is set instead; the view does not clean or drop characters);
    stripped, deduplicated, sorted, cut only at `MAX_NOTICE_CHARS`; it is shown for a create-only
    key too (it is not decision content). At most 4 distinct notices are listed; when there are
    more, `provider_notices_not_shown` holds the count (a number, not text in the list) and the
    web app says "n more provider notices not shown". `provider_notices` holds provider text only.
    A tides.today item (by source and category) whose notice is missing, blank, not a string or
    not printable sets the separate boolean `provider_notice_missing`; the web app then says, in
    its own fixed words and outside the provider-text block, "provider notice missing for a
    tides.today item", and when only the flag is set it shows only that sentence: no section,
    heading, caption or provider text. A provider notice whose text equals that sentence stays
    provider text and does not set the flag. The fixed field `provider_notice_note` ("Text supplied by the data provider,
    shown as received. It is the provider's own copyright and licence notice, shown because this
    assessment retrieved tides.today data (that data may have been withheld; see Evidence, if your
    key can read it). DiveSafe has not checked it. It does not validate the data, does not
    describe the tide at the dive site, and does not change the result.") is set when the view has
    a provider notice or the missing flag, and is "" when there are no notices and the flag is
    false; the web app shows it as a caption under the heading "Provider notice (tides.today)"
    above the provider text only. The notice is the provider's text
    only, separate from `attributions`, and untrusted: the API returns it as a plain string and
    the web app renders it as text. Because the user view cuts attributions at 300 characters, the
    `attribution` field stays a fixed short text naming Tides Today, FES (CNES, CLS, LEGOS /
    AVISO+), ETOPO 2022 (NOAA NCEI) and PSMSL, with a pointer ("Provider notice shown on the
    assessment page."). The full notice is record-only for prompts and traces:
    `agents.prompts.RECORD_ONLY_VALUE_KEYS` drops `provider_notice` from every prompt payload
    (security review 2026-11-17), so only the short `attribution` goes to the model host, and no
    span attribute carries it. Building the view logs nothing (tested).

    **OPEN QUESTION (not settled):** whether showing the notice once per assessment view meets
    the provider's permission wording "with every item" is the owner's call. Switch-on condition
    (1) is not met until the owner has answered it.
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
    *(Superseded by Amendment 1: the V2 exclusions are in decision 2 there; spring/neap is
    no longer available.)*
14. **Off by default.** `DIVESAFE_TIDES_TODAY_ENABLED` defaults to false. Enabling it is the
    owner's decision, taken only once both switch-on conditions in the status are met (the
    permission is on file since 2026-11-18; condition (1) is built, pending review and a manual
    browser render, and needs the owner's answer to the OPEN QUESTION in decision 11; the owner's
    decision to switch on is not taken). Production compose passes it to the
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
- **Show only the short attribution in the user view.** Rejected (owner, 2026-11-19): the
  provider's permission is for the notice shown with every item; decision 11 shows it whole, once per
  assessment view.

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

- V1: units came from the owner's reading of the website and the datum was not stated. V2
  (Amendment 1): metres as named by the field `heightM`; datum CD as stated, its level unknown.
- A prediction for Teluk Tekek, 3.7 km from the Tioman reference point (13.4 km from the
  Tiger Reef dive site, position as published by PADI; Amendment 2), not for any reef. Astronomical prediction only, with no weather or
  pressure effects.
- Which model produced the values: V2 names `tpe` (the provider's engine on FES); not verified.
- `estimated: true` extremes are refused, so a change upstream could make the category fail.
- The plausibility bound on the gap between extremes (18 h) could refuse a real long diurnal
  interval (fail closed; availability, not safety). It does **not** catch a single missing high/low
  pair: alternation still holds and the gap can stay under 18 h
  (`test_known_gap_a_single_missing_high_low_pair_is_not_detected` shows a dropped pair in the
  synthetic V2 fixture leaves a 17 h 20 min gap and is accepted). Unlike PHN, there is no height
  series to cross-check against, so an incomplete list for a day can pass. The items say they list
  the provider's highs and lows, not that the list is complete.
- The call budget is per process and shared by all users; using it up makes tides missing for
  everyone (fails closed). Retrieval time may be up to the cache lifetime older than shown.
- The API version is "v2" (Amendment 1) with no change notice; a shape change makes the category
  fail.
- The sole-source clause (§2) is met by the human gate and by official sources the diver checks;
  the software cannot check that.
- The web page (`web/components/ProviderNotices.ts`) relies on the API for the cap of 4 provider
  notices and for the string check: it does not cut the list itself (no `slice(0, 4)`) and does
  not check that each entry is a string. If the API sent more, the page would be longer; React
  still renders every entry as escaped plain text (security review, optional nit).

## Acceptance tests

Fixture parse; each failure in decision 5 and Amendment 1 decisions 3, 4 and 6; no item at all when
any of the three calls fails; `observation_from_evidence` is `None`; the prompt carries the `tide`
value and the "not a current" wording; an engine test that this evidence alone never gives GO or
CAUTION; the shared getter refuses the host; the tides.today getter refuses other hosts, never
follows a redirect, sends the `x-api-key` header only to its own host (the tides.today host never
accepts `Authorization`, the MET host never accepts `x-api-key`), and the credential never appears
in errors or logs; settings and secret-file tests; the start-up egress warning; the attribution in
the user view. Mutation checks on the location check (id, name and coordinates), the alternation
check, the bracketing check, the horizon and past-window checks, the `estimated` refusal, the 18 h
gap bound, high above low, the time zone, the notice length and printable checks, the single-page
and echoed-dates checks, the ephemeris checks (day count and coverage, location, moon-phase name and
range, sunrise and sunset within their day), the datum (CD only), the `source` block and the
upstream (`tpe`), the host separation (shared list, merged or overlapping credential lists, the
exact header name (not case-folded), the value prefix, a second scheme for either host, and a blank
or line-breaking key value), the record-only filter for `provider_notice`, the full notice in every user view
(`tests/safety/test_tides_today_full_notice.py`), the fixed text of a
validation error, the wiring, the egress warning, and a swallowed failure of each of the three
calls. `test_known_gap_a_single_missing_high_low_pair_is_not_detected` pins the gap listed under
Known limits.

## Amendment 1: API V2 (2026-11-17, owner)

The connector moves from API V1 to V2 (`https://api.tidestoday.io/v2`). `SOURCE_VERSION` is
`api-v2`; `PROMPT_VERSION` is unchanged. Decisions:

1. **Credential.** One API key, sent as the `x-api-key` header. The application ID is removed
   from settings, secret files, Compose, `make-secrets.sh` and the example environment.
   `CREDENTIAL_SCHEMES` binds each credential host list to an exact header name and a value
   prefix: MET `Authorization` + `METToken `, tides.today `x-api-key` + no prefix. A getter whose
   header name or prefix does not match its host list is refused at construction.
2. **Calls (GET only, three per assessment):** `locations/2960?locale=en` (datum, upstream,
   notice); `locations/2960/tide-extremes` with `filter.dateTime=$btw:<first-1>,<last+1>` and
   150 per page; `locations/2960/ephemeris` with `filter.date=$btw:<first>,<last>` and 50 per
   page. No write endpoint, `currentTide`, weather or wave value is used. The budget (6 a minute,
   300 a day) is unchanged.
3. **Datum.** Only chart datum is accepted: `source.datumType` must be `CD`. Anything else, or a
   missing value or `source` block, is refused. The level of CD at this point is not stated.
4. **Upstream.** `source.source` must be `tpe` (the provider's own engine on the FES models).
   This is the provider's statement and is not verified, so `upstream_known` stays `False` and
   the item never corroborates anything.
5. **Units.** Metres, as named by the field `heightM` (the V2 specification documents no unit);
   `heightF` is ignored.
6. **Ephemeris.** Exactly one row per local day in the window, each with `locationId` 2960 and
   sunrise before sunset on that day. `moonPhaseName` must be one of the eight documented names,
   and `moonPhase` a finite number from 0 to 1. Stored as `tide.moon_phase = {name, fraction}`.
   The API does not document what the fraction measures; a quality note says the moon phase is
   astronomical context only, not a spring/neap classification, and says nothing about current
   strength. Moonrise and moonset are dropped.
7. **Spring/neap is dropped.** The V2 endpoint returns 403 for this key. Access is to be asked of
   the provider along with the permission request.
8. **Unchanged from V1:** DEGRADED, off by default, estimated extremes refused, the 18 h gap
   bound, alternation, high above low, whole-day coverage, the single-page, count and echoed-
   request checks, and no item at all if any call fails.

Sources: the V2 OpenAPI specification (enums for `datumType` CD/LAT, `source` including `tpe`,
and the eight `moonPhaseName` values) and the owner's V2 requests of 2026-11-17. The V2 parser
was run locally against those recorded responses (HTTP 200) and accepted them: four extremes for
19 Nov, moon phase "Waxing Gibbous" (the three days returned two of the eight names), and a
1023-character notice; only the echoes were adjusted to the asked dates. Nothing is committed.

Operator step after merging: delete `docker/secrets/divesafe_tides_today_app_id` on each
deployment host; it is no longer mounted. If it was ever filled in, ask the provider to revoke
that application ID.

Known limits updated by this amendment: units are named by the API field, not documented; the datum
is stated as CD, but its level is not; the stated upstream is unverified; the API version is "v2",
with no change notice; spring/neap is not available.

Reviewed by `security-reviewer` and `risk-reviewer` (2026-11-18).

## Amendment 2: Tiger Reef mapped to Teluk Tekek as a proxy (2026-11-20, owner)

The owner ordered the dive site `my-pahang-tiger-reef` ("Tiger Reef (dive site)", 2.89302, 104.05593, the point published on PADI's "Tiger Reef"
dive-site page, which matches the owner's boat GPS reading (undated; supplied 2026-11-20) exactly; see the
position-source bullet)
to be tide-mapped
to location 2960 "Teluk Tekek" (2.8, 104.133003), the same point as the Tioman entry.

- **It is a proxy, not the reef.** The point is 13.4 km from the Tiger Reef dive site as registered (haversine; position as published by PADI), against
  3.7 km for the Tioman reference point. The tide at the reef may differ in height and time from
  the tide at Teluk Tekek; nothing here says by how much, and the data cannot say whether the
  difference matters for any dive.
- **The distance is per site.** `Location` carries a `distance_note`; the fixed `DISTANCE_NOTE`
  is removed. Tioman keeps its wording ("about 3.7 km from the site's reference point") in
  `location.description` and `spatial_scope`. Tiger Reef's items say "about 13.4 km from the
  Tiger Reef dive site (position as published by PADI)". Both still say the item is a prediction for
  the tides.today point, not for the dive site (item `location.description`, `spatial_scope` and
  the quality notes). The one new quality note below is on every site's items, Tioman's included.
- **Nothing else changes.** Evidence stays DEGRADED, upstream unknown, never corroborating, and
  tides.today stays **off by default** (`DIVESAFE_TIDES_TODAY_ENABLED` is not touched). The
  permission text is unchanged; whether it covers Tiger Reef is the owner's reading (below). Tiger Reef is not mapped for PHN
  e-Tide (being removed), the data.gov.my forecast or the MET connectors (their zone tables are
  not yet checked); those refuse it with their fixed "not registered" message.
- **The distance wording for Tiger Reef** is "about 13.4 km from the Tiger Reef dive site
  (position as published by PADI)", because the position is PADI's published one, which PADI does
  not warrant, and no survey was made; the 13.4 km is only as good as that position. Every item, for
  Tioman as well as Tiger Reef, also carries the quality note "the tide at the dive site may
  differ in time and height from this point's by an unknown amount".
- **No search was made around the reef.** Decision 1's "2960 is the only one within 25 km" was
  centred on the Tioman reference point, which is 16.2 km from Tiger Reef. No tides.today search
  was made around Tiger Reef itself, so a location closer than 13.4 km may exist. Finding out is
  open work (it needs the provider's search, which is the owner's access).
- **The owner's reading of the permission (2026-11-20).** The owner reads the provider's written
  permission (on file since 2026-11-18, scope as read on 2026-11-19) as covering Tiger Reef as a
  proxy through this same key and API. This is the owner's reading, not a confirmation from the
  provider; the provider was not asked. The same scope points apply: the owner's own deployment,
  non-commercial use, a qualified human deciding, the data never the sole source, and §2 and §9
  not lifted.
- **Known gap: the site guide is not retrieved for Tiger Reef.** The only loaded site document is
  `tioman-site-guide`, for site `my-pahang-pulau-tioman` (ADR 0024: v1 loaded 2026-11-12; v2 the
  same 1489-character text with a new section name, "Pulau Tioman site guide"; v3, loaded
  2026-11-15 with the model-host attestation, again byte-identical to v2 and APPROVED, review by
  2027-05-13, v1 and v2 WITHDRAWN). The text was not changed by any version and its content is not
  restated in the repository; the only record of what it says about Tiger Reef is ADR 0028's note
  that, in the owner's trial, the guide's hazards included currents at Chebeh and Tiger Reef, so
  that is the owner's report, not something this change checked. Retrieval is
  by the plan's `site_id` (or an all-sites document), so a Tiger Reef run retrieves nothing, the
  site view says "No hazards, local rules or site knowledge are registered for this site", and the
  human at the gate does not see the one reported Tiger Reef current hazard (owner's trial report, ADR 0028). Absence is not evidence
  of no hazard. The owner decides whether a reviewed Tiger Reef (or all-sites) guide is loaded
  through the knowledge loader; this change loads nothing.
- **Known gap: Tiger Reef cannot reach GO.** The data.gov.my forecast connector (always built) and
  the MET marine connector (built when its token is set, and live in production) refuse Tiger
  Reef with their fixed "not registered" message, and so does PHN while it exists. Every Tiger Reef
  run therefore records `evidence.connector_issues`, which makes it INSUFFICIENT EVIDENCE at
  best (NO-GO is still possible), and each refusal adds to the `observe.connector_failed:<name>` counts (metric noise, ADR
  0027). Tiger Reef can never reach GO, even with a future signed-off ruleset, until those
  connectors are mapped for it (their zone tables are not yet checked). That is safe but is not
  the same as a clean run.
- **Tests:** `tests/unit/test_tiger_reef_site.py` (synthetic fixture only), including a table
  test over every `LOCATIONS` entry, marked `safety`: the key is a registered site, the stated
  kilometres equal the haversine distance from that site's own coordinates, the note is not
  blank, and the item says "not for the dive site" and carries the "may differ" quality note.
- **Position source (owner, 2026-11-20):** the coordinates are those published on PADI's "Tiger Reef" dive-site page (https://www.padi.com/dive-site/malaysia/tiger-reef-2/, read 2026-11-20; contributed by a PADI dive centre; PADI does not warrant accuracy), which match the owner's boat GPS reading (undated; supplied 2026-11-20) exactly. The exact match suggests a common origin rather than two independent fixes; no survey was made. The owner's earlier same-day decision to name the site "Tiger Reef (dive site; position unverified)" is superseded. PADI's terms of use were not checked; one coordinate pair was reused by hand, and PADI's stated depth and currents are cited, not copied; nothing is fetched from PADI.
- S6 closed by the owner (2026-11-20): the position is cited to PADI with its disclaimer in `coordinate_source`; the assessment view still does not show `coordinate_source`.
- **PADI also describes strong currents at the site** (a channel between Labas and Sepoi): a public statement of a current hazard, in addition to the owner's trial report (ADR 0028); nobody has checked that the two describe the same hazard. Neither reaches the human: the Tioman guide is not retrieved for Tiger Reef, and nothing from the PADI page is loaded. With two sources naming a strong-current hazard while the site view says no hazards are registered, this gap weighs more; loading a reviewed Tiger Reef guide is the owner's decision. The run still fails closed (never GO).
- PADI's page also lists a maximum depth of 24 m; it is recorded here only as a citation and is not a `SiteConstraint`, rule value or plan default (AGENTS.md section 7).

Reviewed 2026-11-20 by `security-reviewer` (no blockers, no should-fix) and `risk-reviewer` (no blockers), including the PADI position source; S6 closed by the owner (2026-11-20).
