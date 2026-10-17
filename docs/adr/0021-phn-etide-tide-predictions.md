# ADR 0021: Tide predictions from the PHN e-Tide endpoint

Status: PROPOSED (2026-10-17). Replaces the withdrawn ADR 0020. Needs `risk-reviewer` and
`security-reviewer` before it is accepted and before the connector is merged.

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
the terms of the permission; any tidal-stream data (none is in this response).

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
   - the response is not the expected shape, or the body is too large or not JSON.
4. **No rule, no limit.** This adds no tide threshold and no rule. `tidal_current` stays
   unencodable: a tide height or time is not a current speed. With degraded quality it cannot
   support GO or CAUTION (ADR 0008). The visible outcome is unchanged until a dive professional
   signs limits.
5. **Transport.** The endpoint needs a POST with a form body. A separate getter subclass
   (`UrllibFormPostJsonGetter`) with its own host list `PHN_HOSTS = {"etide.hydro.gov.my"}`,
   https only, no redirects followed, no cookies kept, same size cap and deadline. The shared
   `ALLOWED_HOSTS` does **not** gain this host, so no other connector can reach it. No credential
   is sent. The site-to-location table is fixed in code (`my-pahang-pulau-tioman` -> `pulau_tioman`).
6. **Politeness.** PHN publishes no limits. A local budget of 2 calls a minute and 200 a day, and a
   60 second cache, are **our conservative assumptions**, recorded as such, not provider figures.
   The budget is per process.
7. **Attribution and terms.** Each item names "Pusat Hidrografi Nasional (e-Tide)" and says the
   permission is the owner's report and its terms are not on file. If PHN states wording, it
   replaces this. The data is used non-commercially, like every other source here.
8. **Datum.** Heights are carried with their stated datum. No depth, clearance or "water over the
   reef" logic is added.

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
- One harbour point cannot say what the tide does at a reef.
- The time zone is inferred and checked only against our clock on `current_day`.
- Undocumented endpoint: a layout change makes the category fail, not guess.
- Tidal streams are not covered and the `tide_current` specialist still has only coarse
  Copernicus currents.

## Acceptance tests

Recorded-fixture parse; each failure above; `observation_from_evidence` is `None`; the prompt
payload carries the `tide` value and the "not a current" statement; an engine test that tide
evidence alone never gives GO or CAUTION; the shared getter still refuses the PHN host; the PHN
getter refuses other hosts, never follows a redirect and sends the form body by POST; mutation
checks on the clock check, the alternation check and the horizon check.
