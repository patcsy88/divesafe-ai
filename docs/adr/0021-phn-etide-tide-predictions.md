# ADR 0021: Tide predictions from the PHN e-Tide endpoint

Status: PROPOSED (2026-10-17). Replaces the withdrawn ADR 0020. Reviewed by `security-reviewer` and
`risk-reviewer` on 2026-10-17, then re-reviewed against the fixes (commit `3e239e3`): no blockers
and no Major findings; the minor findings are folded in below. It stays PROPOSED, and
the connector stays **off by default** (`DIVESAFE_PHN_ETIDE_ENABLED=false`), until the owner puts
PHN's wording (or a dated record of the permission) on file and accepts this ADR.

## Context

`tides` is a required category in the interim ruleset (`api/state.py`) and has had no source, so
every assessment is INSUFFICIENT EVIDENCE for that reason as well as for the missing signed limits.
The owner asked on 2026-10-17 to include tide predictions, dropped TICON-4, and reported that
Pusat Hidrografi Nasional (PHN, the National Hydrographic Centre) gave permission to use its
e-Tide endpoint. The wording of that permission is **not on file**; only the owner's statement is.

The e-Tide page (`etide.hydro.gov.my`) lists Pulau Tioman. Its script POSTs `fetch_data=&location=
pulau_tioman` to `./functions/q_functions.php`. One request was made on 2026-10-17 with the owner's
go-ahead and saved outside the repository; a one-day trim is committed as
`tests/fixtures/phn_etide_tioman_recorded_2026-10-17.json` (the owner agreed to that).

## What was observed (one response, 2026-10-17)

- HTTP 200, about 28 KB, `Content-Type: text/html` with a JSON body, and a session cookie.
- Three blocks, `current_day`, `prev_day`, `prev_1_day`. Despite the names, the latter two were the
  **next two days** (18 and 19 Oct). So the horizon is today plus two days.
- Per day: 144 heights on a 10-minute grid, and `MAX_LOW_Tide.HL`, a list of
  `{date, time, height, stat}` with `stat` 1 for high and 0 for low (four per day that day).
  Every listed extreme was a local maximum or minimum of the series.
- `MAX_LOW_Tide.C_status` is `{status, time, height}` for "now". On the two future days it repeated
  today's clock time, so it is a placeholder there.
- The page note says heights are **Lowest Astronomical Tide / chart datum**, in metres. The values
  (0.97 to 2.95) fit metres. The note is on the page, not endpoint documentation.
- Time zone: the server `Date` header was 08:02 GMT and `C_status.time` was 16:02, so +08:00. That
  is one observation, not a documented guarantee.

## Not known

Rate limits; refresh cadence; whether values come from PHN's published tide tables or a model;
stability (no versioning, a page change could change the layout); the units beyond the page note;
the terms of the permission; any tidal-stream data (none is in this response); which station
the response is for (it names none).

## Decision (proposed)

A `PhnEtideConnector` reads PHN's own high/low list for the days a dive window touches.

1. **Evidence.** One item per day: category `tides`, `data_kind` PREDICTION, `source`
   `phn-etide-prediction`, quality `DEGRADED`, `upstream_known=False` (the producing method is not
   stated, so it never corroborates anything, ADR 0010). `valid_at`/`valid_until` are that calendar
   day at +08:00. Retrieval time is the only age we know.
2. **Value.** Under the key `tide` only, never `variables` or `ranges`, so no hourly rule and no
   typed observation can read it: `datum` ("LAT/chart datum per the page note"), `unit` ("m"), and
   `extremes`, a list of `{time (ISO, +08:00), kind (high/low), height_m}` in time order. The
   10-minute series is validated and then **dropped** (it would bloat prompts and adds nothing the
   extremes do not). The value states that it is a tide height and time only and says **nothing
   about current speed or slack water**.
3. **Fail closed.** A connector error makes the `tides` category missing, so the result is
   INSUFFICIENT EVIDENCE. It raises, and never returns partial data, when:
   - the window is not inside today and the next two days (MYT), or starts before today;
   - the block dates are not today and the two following days, or a block's date disagrees with its
     rows;
   - a needed day is missing, a height is not a finite number in a data-sanity range, the series is
     not exactly 144 slots on the 10-minute grid, or an extreme is not a local extreme of that
     series at that time, does not alternate high/low, or is out of order;
   - `C_status.time` on `current_day` is more than 30 minutes from our clock (this is the only
     runtime check on the inferred +08:00 zone; 30 minutes is a plausibility bound, not a safety
     limit);
   - the high/low list is incomplete: between consecutive listed extremes (and from each day edge
     to the nearest extreme) the series must run one way, so a dropped high or low is refused (no
     tolerance; run on all three days of the one real response, of which only 17 Oct is committed; the
     full response has SHA-256 `af1f0eca53c6d7b5cdcc7a66878288cc4b6f04f3846fc35c9273398f6a223bb8`);
   - highs and lows do not alternate across midnight between two fetched days;
   - the response is not the expected shape, is larger than 512 KiB, is not JSON, or is nested more
     than 32 levels (a parsing bound; a hostile body can otherwise overflow the stack in the cache's
     deep copy and abort a run).
4. **No rule, no limit.** This adds no tide threshold and no rule. `tidal_current` stays
   unencodable (it has no metric, and no metric reads the `tides` category; a safety test pins
   both). The `tidal_current` placeholder no longer cites tide ids. GO stays structurally
   unreachable: `tidal_current` has no data source, and `tides` is required while only DEGRADED
   predictions exist. **Turning on `degraded_may_support_go` is a separate risk decision, it is
   global (it would also admit MET daily ranges and coarse model currents), and it must not be
   taken for tides.** Making the degraded policy per category is a follow-up to ADR 0008.
5. **Transport.** The endpoint needs a POST with a form body. A separate getter subclass
   (`UrllibFormPostJsonGetter`) with its own host list `PHN_HOSTS = {"etide.hydro.gov.my"}`,
   https only, standard port and no userinfo, no redirects followed, no cookies kept (so each call
   starts a new PHN session), a 512 KiB size cap and the usual deadline. The GET getter refuses the
   PHN host, so the separation holds both ways. The shared
   `ALLOWED_HOSTS` does **not** gain this host, so no other connector can reach it. No credential
   is sent. The site-to-location table is fixed in code (`my-pahang-pulau-tioman` -> `pulau_tioman`).
6. **Politeness.** PHN publishes no limits. A local budget of 2 calls a minute and 200 a day, and a
   60 second cache, are **our conservative assumptions**, recorded as such, not provider figures.
   The budget is per process.
7. **Attribution and terms.** Each item carries `attribution` "Pusat Hidrografi Nasional (e-Tide)",
   which the user-facing attributions list shows, and says the
   permission is the owner's report and its terms are not on file. If PHN states wording, it
   replaces this. The data is used non-commercially, like every other source here.
8. **Datum.** Heights are carried with their stated datum. No depth, clearance or "water over the
   reef" logic is added.

9. **Agents.** The `tide_current` focus text no longer invites slack or flow timing from tide
   data. Trusted wording outside the untrusted block (specialists that read tides, the three
   scenarios and the risk proposal) says tide items are not current, slack-water or flow-timing
   evidence whenever a tide item is present. `PROMPT_VERSION` is `2026-10-17.2`.
10. **Off by default.** `DIVESAFE_PHN_ETIDE_ENABLED` defaults to false. Enabling it is the owner's
    decision after the permission wording is on file.

## Owner decisions recorded here

- The one-day fixture is committed on the owner's say-so; PHN's redistribution terms are not on file.
- The user agent carries no contact address. Adding one is the owner's call.

## Options not taken

- **Read the e-Tide web page as HTML or scrape other PHN pages.** Not done; only the endpoint the
  owner said is permitted.
- **Derive extremes from the series.** Rejected: flat stretches make this ambiguous (on 19 Oct a
  low and a high are 1.1 h and 0.01 m apart). PHN's own list is used and cross-checked against the
  series.
- **Use `C_status` as a current level.** Rejected. It is a placeholder on future days and is read
  only for the clock check.
- **Add the host to `ALLOWED_HOSTS`.** Rejected: it would let every default-getter connector call it.
- **Store the 144-point series.** Rejected for prompt size and no benefit while there is no rule.

## Known limits (to be carried into `docs/risk-model.md`)

- Horizon is today plus two days. A planned dive later has no tide evidence.
- Astronomical prediction only, with no weather or pressure effects.
- One PHN station cannot say what the tide does at a reef. **The response does not name its
  station**; that it is the `pulau_tioman` station is assumed from our request, and items say so
  (`requested_location_id`). No check is possible until PHN gives a station identifier.
- An extreme at a day edge or on a flat stretch is checked against one side or an equal-height
  stretch only, and its time is PHN's choice within that stretch.
- Retrieval time may be up to the 60 s cache lifetime older than shown.
- The call budget is per process and shared by every signed-in user; about 200 runs more than a
  minute apart use up the 24-hour budget and tides then go missing for everyone (fails closed).
  Concurrent cache misses are not coalesced.
- A constant stretch inside the series passes the completeness check (equal neighbours are
  allowed, which the recorded plateaus need); a flat-lined or padded series is not detected.
  No bound is invented here.
- A real small turn that PHN does not list makes the whole category fail (fail closed; availability,
  not safety). No tolerance is added.
- Environment proxy settings are honoured by every connector getter, as before.
- The time zone is inferred and checked only against our clock on `current_day`.
- Undocumented endpoint: a layout change makes the category fail, not guess.
- Tidal streams are not covered and the `tide_current` specialist still has only coarse
  Copernicus currents.

## Acceptance tests

Recorded-fixture parse; each failure above; `observation_from_evidence` is `None`; the prompt
payload carries the `tide` value and the "not a current" statement; an engine test that tide
evidence alone never gives GO or CAUTION; the shared getter still refuses the PHN host; the PHN
getter refuses other hosts, never follows a redirect and sends the form body by POST; mutation
checks on the clock check, the alternation check and the horizon check; also the completeness and
cross-midnight checks, the depth bound, port and userinfo refusal, the tide-is-not-a-current
wording outside the untrusted block, the PHN attribution in the user view, and the station
wording (`requested_location_id`).
