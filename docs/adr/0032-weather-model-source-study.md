# ADR 0032: Weather model source study (OpenWeather One Call 4.0 and Storm Tracker)

Status: PROPOSED (2026-11-23), **study only**, draft 10. Nothing is registered, built or wired.
The owner holds a key, created by hand (row 230: no agent created it). This study key is used
only for checks; before switch-on the owner deletes it and creates a new key for the connector,
stored only in the secret file (owner decision #62a). No subscription to One Call has been
taken. The only calls were the owner's licence-check calls of 2026-11-23 (see "Licence and price
check"); no DiveSafe code made a call and no provider data was kept. The owner
answered the study's open questions on 2026-11-23 (decision log rows 212 to 222) and added two
decisions after review (rows 229 and 230). All are folded into the decisions below and listed
under "Owner decisions (2026-11-23)". Registering a source stays the owner's decision, after the
licence and price check (owner decision #19a and row 212). Drafts 2 to 7 were reviewed by
`security-reviewer` and `risk-reviewer`, and their findings are folded in (see "Review
history"). Draft 9 was confirmed by both reviewers with no blockers and no should-fix items;
see "Conditions before acceptance, build or switch-on". Statements marked "checked on
origin/main" were read from that branch on 2026-11-23, by this study or for the draft 3 and
draft 4, draft 5, draft 6 and draft 7 reviews. Line numbers are as of that check and may move.

## Context

`weather` is one of the four threshold factors that cannot be encoded (ADR 0009). Its
`ThresholdTbdRule` placeholder always reports the factor as not evaluated, and the coverage guard
(`ruleset.threshold_factors_uncovered`) keeps GO and CAUTION unreachable while it stays so
(risk-model.md, "Risk factors and placeholders"). Today the only weather inputs are:

- MET Malaysia warnings, as free text, which `WarningNeedsHumanReadingRule` turns into
  INSUFFICIENT EVIDENCE (ADR 0005);
- MET's daily general forecast for the island through data.gov.my, context only, daily, with no
  hourly values and no condition code (data-sources.md). Its items are stored in the `weather`
  category (decision 17);
- MET's daily marine ranges for wind and waves, which may only tighten (ADR 0031).

There is no hourly weather data and no structured thunderstorm signal. A thunderstorm in the dive
window reaches the record today only as warning text, and only if MET issues a warning.

The owner asked on 2026-11-23 for a study of one candidate source (OpenWeather One Call API 4.0)
and of a second product from the same provider (Storm Tracker). The study includes a new rule
type that can only add NO-GO. The facts below paraphrase the provider's public documentation,
read on 2026-11-23. Nothing was copied, and nothing was checked against a real response.

## What the provider documents (read 2026-11-23; calls: see "Licence and price check")

### One Call API 4.0

- **Sections:** current conditions; a 1-minute series for the next hour; a 15-minute series and
  an hourly series out to 48 hours; a daily outlook further out; and national alerts.
- **Horizon:** the 15-minute and hourly series end at 48 hours, as documented. A dive window
  beyond the end of the data has no time-resolved data from this source, and its result stays
  INSUFFICIENT EVIDENCE, as for any other missing evidence (ADR 0008). The connector reads the
  end from the data, never from a constant (decision 5). The daily outlook is never read in its
  place (decision 6).
- **Sample times:** each series sample carries a time. Whether the provider means it as an
  instant or as the start of an interval is to be confirmed (decision 5).
- **Location echo:** responses of this family echo a latitude and longitude. Whether 4.0 does,
  and how it rounds them, was not checked (decision 5).
- **Fields of interest.** Meanings are as documented. Units are to be confirmed against a real
  response, which is possible only after the licence check (decision 13).
  - `pop`: probability of precipitation, 0 to 1. A probability, not an amount; no rule reads it.
  - `rain.1h`: rain amount in mm per hour. **Present only when rain is forecast.** An absent
    value means "no data", never zero.
  - `visibility`: metres, capped at 10 km. **Air** visibility, never underwater visibility (the
    same caution as for Open-Meteo's `visibility`).
  - `wind_gust`: gust speed, "where available". ADR 0009 has no gust rule. An absent value means
    "no data".
  - `weather.id`: a weather condition category code, documented as an integer. It is a category,
    not a quantity, and today's rules compare numbers only. Decision 5 checks it strictly.
- **Alerts:** the documented list of national alert sources includes Singapore, Indonesia and
  Thailand, and **does not include Malaysia**. An empty alerts section for a Malaysian site
  therefore says nothing about Malaysian warnings. MET Malaysia stays the warning source
  (ADR 0005); see decision 7.
- **Kind of data:** output of the provider's own proprietary model and forecast process. The
  documentation read does not state the producing model, its grid or its run time.
- **Access:** an API key. The provider also offers a self-signup lane meant for software agents;
  see decision 9.
- **Pricing:** charged per call after a free daily allowance. Each extra page of a response
  counts as a further call. Checked on 2026-11-23; see "Licence and price check".

### Storm Tracker

- Tropical cyclones as GeoJSON: the storm's position, the zone of 34-knot winds, a forecast
  track of up to 10 days, and a cone built from the spread between forecast models.
- Basins: the South China Sea falls in its Western Pacific basin. Whether the provider documents
  the basin's boundary precisely enough to test a point against was not checked (decision 26).
- **What it is not:**
  - It does **not** cover ordinary thunderstorms, squalls or local convection. A dive can meet a
    severe thunderstorm with no cyclone anywhere in the feed.
  - Its cone is **model-spread uncertainty for a cyclone's track only**. It is not a measure of
    general forecast uncertainty, and it says nothing about the uncertainty of wind, waves, rain
    or any other factor. The `forecast_uncertainty` factor stays unencodable.
  - "34 knots" is the name of the zone the provider draws. It is not a DiveSafe limit. This ADR
    adopts no wind number, and the code never contains one.

## Options considered

1. **Do nothing.** Weather stays a placeholder, and thunderstorms reach the record only as MET
   warning text. This is safe (it fails closed), but the rules cannot see a forecast thunderstorm
   that has no MET warning.
2. **Numeric definitions on One Call fields through ADR 0009** (for example a `rain.1h` limit).
   Rejected for this study. It needs signed numbers that do not exist. A second weather metric
   would also break "one primary metric per factor": a rain-only definition would mark the whole
   `weather` factor as covered (ADR 0009, Not solved). And `rain.1h` is absent when no rain is
   forecast, so every band would have to treat "absent" as "no data".
3. **A "condition present" rule type that can only add NO-GO** (owner decision #20a), plus a
   **NO-GO-only cyclone-zone signal** (owner decision #21a). Chosen for study. Neither can
   produce GO or CAUTION, so neither can relax anything, and both fail closed on missing data.
4. **Let an agent read the category or the storm feed.** Rejected. Agents interpret and propose.
   A hazard signal of this kind belongs in the deterministic layer (AGENTS.md section 5).

## Decision (study; nothing to build until accepted)

### A. Source and connector (if registered later)

1. **Candidate only.** One Call 4.0 and Storm Tracker are listed as **Candidate** in the source
   register. Whether to register either is decided by the owner only after the licence and price
   check (row 212; condition 1), and is recorded in data-sources.md. Until then there is no
   connector, no setting, no secret file and no fixture from a real response.
2. **Data kind and quality.** Every item is forecast or model data (`data_kind` `forecast` or
   `model`, `is_forecast=True`). Nothing is ever stored as an observation, including the
   "current" section, because it comes from the provider's model process and not from a gauge we
   can name. Quality is `degraded`, and `effective_quality` caps it at DEGRADED anyway
   (ADR 0008). Lineage: `upstream_known=False`, so it never corroborates any other source
   (ADR 0010). A disagreement with Open-Meteo or MET is not detected (the same gap as
   risk-model.md, "Contradictory evidence").
3. **Degraded data and GO (binding, row 217; broadened after risk review).** Production runs
   with `DIVESAFE_DEGRADED_MAY_SUPPORT_GO=true`: ADR 0008 Amendment 1 made it a setting, and
   ADR 0024 A8 records the switch-on with the ADR 0031 deploy. risk-model.md says the setting is
   off by default (checked on origin/main, near line 240: it says only that, and does not say
   that production leaves it off). The two statements agree: the default is `false`, and
   production sets `true`. The two rule types in this ADR never give GO or CAUTION, so the flag
   does not matter to them.
   - **The hold.** Before any numeric `weather` definition is accepted, **from any source** (One
     Call or any other), `weather` joins `risk.DEGRADED_NEVER_SUPPORTS_GO`. DEGRADED weather
     evidence then never supports GO or CAUTION, whatever the setting says. This is a reviewed
     constant change, made **in the code change that adds a weather metric**, or before it.
   - **Why the constant alone is not enough (checked on origin/main).**
     `DEGRADED_NEVER_SUPPORTS_GO` is defined in `src/divesafe/risk/engine.py` (today `{TIDES}`).
     It is applied in `_quality_guard` only over `usable`, which `_sufficiency` fills from
     `required_categories` only. So the hold acts only on a required category, as ADR 0008
     Amendment 1 states for `tides`. `weather` is not required (row 218), so adding it to the
     constant alone would have no effect.
   - **Preferred way to make the hold act without `weather` being required (draft 4 risk S1,
     S4; draft 5 risk S1).** It is checked in the citation guard (ADR 0008, "A GO or CAUTION
     may not cite unusable evidence").
   - **Which results are checked.** For each held category, a GO or CAUTION result is checked
     when it cites any item in that category, or when its factor reads that category.
     - A result's factor reads a category when the category is in the shared
       factor-to-categories map's set for that factor, or is the category of the metric of the
       definition behind the rule (`SUPPORTED_METRICS[metric].category`,
       `domain/rule_definition.py` line 77, checked on origin/main) (draft 6 risk N1).
     - Both are read from the rule object that produced the result, never from fields the
       result supplies.
     - A rule with no factor (and so no factor on its result) is checked only when its result
       cites the category (draft 7 risk N3). That is safe: such a result covers no factor, so
       the coverage guard still needs a checked result for `weather` before any GO.
     - The map is today's `_FACTOR_CATEGORIES` (`risk/placeholders.py` line 30). It holds a set
       of categories per factor, not one. An invariant test keeps it in agreement with
       `FACTOR_METRICS` (draft 6 risk N2).
     - That map moves from a private name to one shared, reviewed constant.
     - A result that is not checked for a category is unaffected by that category's hold.
   - **What a checked result needs.** For that held category, a checked result:
     - may not cite DEGRADED evidence in that category;
     - needs the VALIDATED items it cites in that category to cover the window on their own,
       using the same coverage test as `_quality_guard`;
     - must cite at least one item from that category if its factor reads it.

     A breach gives INSUFFICIENT EVIDENCE with the result id
     `evidence.quality_held_category.<rule id>`, one per offending result. The name follows
     the `risk-reviewer` draft 4 proposal; the route's scope was settled in the draft 5 review.
     It starts with `EVIDENCE_QUALITY_PREFIX` (`"evidence.quality"`, `risk/engine.py` line
     141, checked on origin/main), which is in `ENGINE_RULE_PREFIXES` (line 176). So the id is
     reserved, and `api/schemas.py` (line 498) shows its rationale under "Evidence problems".
     The rationale is fixed text that names the category, never the offending rule's words.
   - The check is category-wide. It applies to every category in the constant, so it also
     covers `tides`, for results that cite a tide item or whose factor reads `tides`. That is
     harmless while `tides` is required (it is in `INTERIM_REQUIRED_CATEGORIES`,
     `api/state.py` line 74, checked on origin/main): it is then already held through `usable`.
     If a ruleset does not require `tides`, the route is its only hold, which can only tighten
     (draft 6 risk N3).
   - Only `forecast_uncertainty` reads `tides`: its set holds every category, and the map leaves
     `tides` out of every other factor on purpose (ADR 0021). So the route checks
     `forecast_uncertainty` results against every held category. That widens checking and can
     only tighten. So a future `forecast_uncertainty` rule that gives GO or CAUTION must cite
     at least one item from every held category and meet the hold for each (draft 7 risk N2).
     This costs availability only. The factor has no rule today.
   - The alternative, making `weather` required, is decided with the first numeric weather
     limit (row 218). `risk-reviewer` checks whichever is chosen.
   - **Today's enforcement.** A numeric `weather` definition cannot load at all, because ADR 0009
     refuses a factor with no supported metric and `weather` has none (`FACTOR_METRICS`).
     Mutation-checked safety tests keep the metric and the hold together:
     - an invariant: if `WEATHER` is in `FACTOR_METRICS`, then `WEATHER` is in
       `DEGRADED_NEVER_SUPPORTS_GO` (it holds trivially today, and turns red if a metric is added
       alone);
     - engine tests, each run with the setting on and again with it off, with `weather` not
       required. A weather GO result gives INSUFFICIENT EVIDENCE if it cites DEGRADED weather
       evidence, if it cites a VALIDATED weather item outside the window while DEGRADED items
       cover the window, or if it cites no weather item. A GO on another factor that cites
       nothing in a held category is not flagged for that category.
4. **Category; wind stays out (row 216).** One Call items go into `weather` only. One Call wind
   and gust values **may** enter the `wind` category later, but only through a separate ADR, and
   they stay out of this study. That ADR must solve at least:
   - a **second wind source** beside Open-Meteo (and MET's daily ranges), with unknown lineage;
   - its **undetected disagreement** with Open-Meteo, which today could not tighten anything
     (risk-model.md, "Contradictory evidence");
   - **no gust rule**: ADR 0009 encodes wind speed only, and a gust-only definition would mark
     the whole wind factor as covered.
5. **What a connector would read.**
   - **Series.** The hourly and 15-minute series only. Each item is tagged with its series
     (hourly or 15-minute), and the rule reads the tag (decision 17). The `current` section, the
     1-minute series and the daily outlook are never read.
   - **Window.** The dive window plus the nearest sample on each side (the ADR 0009 bracketing
     rule). The horizon is read from the end of each series in the data, never from a constant
     48 hours in code.
   - **Samples are instants (N4).** Each sample is stored as an instant at its documented time,
     and coverage of the window is by bracketing (decision 17). If implementation finds that the
     provider means intervals, samples are stored as intervals instead, and coverage then means
     that the intervals chain from the window start to the window end with no gap. That finding
     is recorded in this ADR and reviewed by `risk-reviewer` before build. Each series has one
     recorded time model (instants or intervals), and each item records it. An item of the
     other model gives INSUFFICIENT EVIDENCE (decision 17).
   - **Completeness.** Every step of a series must be present. A missing step, a malformed
     `weather` entry or an unknown category code rejects that series and is reported as an
     issue. Partial data is never returned as complete.
   - **Non-finite numbers (SF3).** `data/http.py` parses with plain `json.loads` (line 425,
     checked on origin/main), which accepts `NaN` and infinities. A non-finite number anywhere in
     a value the connector keeps rejects the series. The check is in this connector, not in the
     shared getter; changing the shared getter would need its own review.
   - **Strict codes (SF7).** Each category code must be a strict integer: not a bool, not a
     float and not a numeric string. A non-integer code rejects the series. Each sample's
     condition list has a fixed maximum length, which is a parsing bound in `data/` and not a
     safety limit. A longer list rejects the series.
   - **Unknown codes.** An unknown code is rejected, never passed on as "not listed", so a new
     provider code cannot silently fall outside the signed list. The table of known codes is
     built only at implementation, from the provider's documentation, after the licence check.
     It is not in this ADR.
   - **Pages (SF2).** Page URLs are built from the fixed base and a page number, never from a
     URL found in the response. A fixed maximum number of pages per assessment caps a read. It is
     a parsing bound in `data/`, not a safety limit. Reaching it rejects the series, because a
     cut series is never complete.
   - **Location (S7, security N5).** Each item records the site id, the requested point (the
     site's registered point, decision 11) and the coordinates the response echoes back.
     - The connector compares the echo with the request. They must be equal after applying only
       the provider's **documented** rounding, held as a cited format rule in `data/`. That is
       not a safety limit, and it is never an invented tolerance.
     - If the provider documents no rounding, the echo must equal the request exactly.
     - A missing echo, or one that does not match, rejects the series. This fails closed, at a
       cost to availability (Known gaps).
     - If the provider rounds the point, the data describe the rounded point. The connector
       records that as a transformation step, like the marine connector's grid snap
       (draft 4 risk N7).
     - Each item records all three: the site id, the requested point and the echoed point. Only
       the connector compares the echo. The rule compares the site id and the requested point
       exactly, and checks only that the echo is present (decision 17). So the exact match of
       risk review S7 and N3 holds, and the echo check gives it teeth.
   - **What an item keeps (draft 5 security N1).** The connector builds each item's value from a
     fixed list of keys: the series tag and its time model, the sample time, the condition
     codes, the numeric fields named in "Fields of interest", their units, and the three
     location fields (site id, requested point, echoed point).
     - Provider free text (a condition's description, label or icon) is not kept.
     - **Units (draft 6 security N3).** A unit is the connector's own fixed value for that field.
       It is written only after the response's unit has been checked against it. A response
       unit that differs rejects the series. The provider's unit string is never copied.
     - Any key not on the list is dropped. So the generic prompt branch of `_payload_item`
       only ever sees listed keys. `weather` has no typed form: `observation_from_evidence`
       returns `None` for it (`domain/observations.py` line 114, checked on origin/main), so
       that branch passes the value through.
6. **The daily outlook is not used** for a dive window. A daily item may cover the window, but
   it cannot stand in for time-resolved data (ADR 0008 Amendment 1, `risk.COARSE_SPAN`). A plan
   beyond the end of the series in the data therefore stays INSUFFICIENT EVIDENCE for this
   source.
7. **Alerts are not used.** The alerts section is not requested if the API allows that, and is
   ignored otherwise. An empty or absent alerts section is never recorded as "no warnings" and
   never produces an item like `no_active_warnings`. MET Malaysia warnings stay the only warning
   source, through `WarningNeedsHumanReadingRule`.
8. **Host, paths and HTTP (SF1, N2; draft 3 security SF1).**
   - **Host:** `api.openweathermap.org` only, https only, **no redirects**.
   - **Paths:** only the One Call 4.0 path and, if approved (condition 2), the Storm Tracker
     path. The credentialed getter:
     - compares the URL path **exactly** with its fixed list: no prefix match, no percent
       decoding and no normalising (of `..`, `//`, a trailing slash or letter case);
     - refuses any other path before connecting;
     - refuses a URL that already has a query, a fragment or a `;` parameter;
     - refuses a `params` entry whose name equals the key parameter's name, compared
       case-insensitively;
     - never puts the key in the host or the path.

     This is new. `check_url` (`data/http.py` line 62, checked on origin/main) checks the
     scheme, host, port and userinfo, but not the path or the query. `_label` (line 57) keeps
     the path, which is one more reason the path never carries a secret. A test pins the path
     list and each refusal.
   - **A second host:** Storm Tracker's host was not confirmed. If it is not
     `api.openweathermap.org`, the owner decides once it is known (row 215; condition 2). It is
     not refused in advance, and it is never added without that decision and a security review.
     A second host gets its own path list. A host that redirects is refused; it is never allowed
     by loosening the no-redirect rule. No wildcard subdomains are allowed.
   - **Limits:** the existing connector HTTP limits in data-sources.md also apply: the response
     cap, the total deadline and the dedicated pool. Errors carry fixed error text (decision 9).
9. **The key (B1, N3, row 230).**
   - **No agent signs up.** DiveSafe **must not** use the provider's agent self-signup lane.
     Row 230 makes the rule general. In the same docs change as this ADR, AGENTS.md section 12
     gains: "No agent, project or runtime, creates an account, key or subscription with any
     provider, or accepts terms on the owner's behalf."
   - **The owner holds it.** The owner creates any key by hand, in the owner's name, and stores
     it as a secret file in `DIVESAFE_SECRETS_DIR`, like the MET token. It is read into a
     `SecretStr` setting with a shape validator, like the MET and tides.today keys.
   - **Rotation.** The key is a billing credential. Rotation is one documented step: the owner
     replaces the secret file, recreates the API container and revokes the old key at the
     provider. The step is recorded in ADR 0024 when the source is switched on (condition 9).
   - **What the existing getter does (checked on origin/main).**
     - `src/divesafe/data/http.py` sends credentials as headers only (`CREDENTIAL_SCHEMES`,
       `add_unredirected_header`).
     - `_get` builds the full URL with its query. In `_open_json` (line 376), `HTTPError` and
       `URLError` are raised `from exc` (lines 402 to 413), so a chained exception keeps that
       URL.
     - The header-echo path (lines 416 to 424) sets an `echoed` flag inside the `except` block
       and raises only after the block has ended, so its exception carries no chained error.
     - The cache key in `ratelimit.py` includes `params`.
     - Checked for the draft 3 review with a local Python run: `raise X from None` inside
       `except HTTPError` still leaves `__context__` set to the `HTTPError`, whose `.url` holds
       the full query. `from None` only hides the chain when a traceback is printed; it does not
       remove it.

     Draft 2 said error text was "already stripped of the query" and compared this source to
     the MET credentialed getter. Draft 3 proposed `from None`. All three were wrong for a key in
     the query (B1).
   - **Where the key goes.** In a header if the provider accepts one; in a query parameter only
     if no header form exists. A key in the query is also kept in the provider's request logs.
     That is one more reason for rotation (draft 4 security N1). In either case a dedicated
     credentialed getter adds the key inside the request step, below the cache and budget
     layer. So the key is never in `params`, in the cache key or in connector code. The getter:
     - is bound to the provider's host and path list (decision 8), kept apart from
       `ALLOWED_HOSTS` (as `CREDENTIAL_SCHEMES` is);
     - refuses redirects;
     - raises every error with fixed text **after its `except` block has ended**, following the
       header-echo pattern, so every exception leaving the getter has `__cause__ is None` and
       `__context__ is None`.
   - **The client.** It builds on the existing urllib getter in `data/http.py`. No new HTTP
     client (such as `httpx`) is added.
   - **No provider text in errors (draft 4 security SF1).** Issues from these connectors carry
     fixed text, the HTTP status and, for a rejected payload, a fixed reason code (for example
     `geometry.ring_not_closed`, `series.step_missing` or `code.unknown`). They never carry the
     `str()` of a parse or validation error, because that echoes input values. Today's
     connectors do carry it: `data/open_meteo_marine.py` line 130 (checked on origin/main)
     raises `unusable response: {clip(exc)}` `from exc`. Like the getter, these connectors
     raise after the `except` block has ended. Issues reach the record through the
     `evidence.connector_issues` rule (`orchestration/pipeline.py` line 82).
   - **Never from a handler (draft 4 security N5).** The getter and the connector are never
     called from inside an `except` or `finally` block, so no exception they raise can pick up
     an outer `__context__`.
   - **The cache and budget layer (draft 5 security N3).** These connectors call through
     `data/ratelimit.py`, which today raises `from exc` on `RecursionError` (lines 124 to 125,
     checked on origin/main). That layer also raises with fixed text after its `except` block
     has ended, or the canary's chain assertion states exactly which layers it covers. The
     assertion is never loosened to make the test pass. The change to `ratelimit.py` is part of
     this ADR's build, under its own review.
   - **Proof.** The key is never logged, traced, echoed, written to a fixture or stored in a
     record. The canary-key test (see Tests) checks this. A `str()` or `repr()` check is not
     proof on its own: the test also checks the exception chain and the attributes of every
     reachable exception. Frame locals are a remaining gap (Known gaps).
10. **Budget (SF3).**
    - The local budget per process is the free daily allowance divided by the largest possible
      number of processes, with a margin. The numbers come from the owner after the price check.
      None is invented here.
    - Calls are counted before they are made. Failed and refused calls count, and so does every
      extra page.
    - If the provider offers an account spending cap or call cap, the owner sets it at or below
      the free allowance.
    - A used-up budget fails closed as a connector issue (INSUFFICIENT EVIDENCE), as for
      Open-Meteo and MET.
    - The budget is per process and not shared, and an overrun costs money, so this is
      condition 4 rather than a Known gap.
11. **What a request carries (SF4, security N3).** A request carries only the site's registered
    coordinates and fixed parameters (and the key, as in decision 9). It never carries plan text,
    the site name, the dive time, the actor, the key id or the request id, in the query or in a
    header. A test pins the exact parameter set and checks that no header carries any of these.
    An item never holds the request URL, query or headers, in `source_version`,
    `quality_notes`, `transformations` or any other field. ADR 0024 "What leaves the machine" is
    updated in the change that switches the source on (condition 9). That update states that
    values go to the model host and coordinates go to OpenWeather.
12. **Egress (SF8, security N4).**
    - If connected, One Call values go to the model host with each assessment and are kept in
      stored records and backups (ADR 0024, "What leaves the machine"). The licence must allow
      all three (condition 1).
    - Raw Storm Tracker polygons are never sent to the model host. Only the rule result and, at
      most, the storm name and the zone times are sent.
    - **Own prompt envelope (draft 4 security SF2).** `tropical_cyclones` items get their own
      envelope in the prompt layer, built like `_knowledge_item`. It holds the id, category,
      validity times, data kind and effective quality. From the value it holds only the
      clipped storm name (see below) and the zone times. Any key not on the envelope's list is
      dropped. These items never take the generic branch of `_payload_item`
      (`agents/prompts.py` line 125, checked on origin/main), which passes the value through.
      The tests are listed under Tests. This needs `ai-engineer` to build and
      `security-reviewer` to review.
    - **Storm names (draft 5 security N4, draft 6 security N1).** The storm name must be a
      string, checked with `bounded_text` (`data/parsing.py` line 40, checked on origin/main).
      Any other type rejects the feed with a fixed reason code. The name is then passed through
      `clip()` (line 14) with a fixed length, which replaces control and format characters.
      Without the type check, `clip()` would turn a non-string into its `repr()` (line 16). The
      name is kept as text, never parsed or matched, and never reaches a rule. In prompts it is
      delimited as untrusted text (AGENTS.md section 12). It is not the `kb_screen` id screen.
      A `tropical_cyclones` item keeps no other free text (decision 24).
13. **Fixtures (rows 213 and 229; security SF4).**
    - Row 213 allowed recorded real responses as fixtures. Row 229 narrows that.
    - **Recorded OpenWeather responses stay outside the repository**, as for tides.today
      (data-sources.md).
    - The committed fixture stays **SYNTHETIC** unless the terms explicitly allow redistribution
      in a source repository. A SYNTHETIC fixture is labelled as such, shaped after the
      documented schema, holds no provider values and is never presented as recorded
      (AGENTS.md section 10).
    - **Only the body of a successful 2xx response is recorded**, never an error body, and never
      the request URL or headers. The owner records it, from the owner's shell; an agent never
      does. The owner's command reads the key from the secret file (as a header, or through a
      config file the tool reads), never as text on the command line, where shell history and
      the process list would keep it.
    - No recording is kept at all until the licence check confirms that storing provider data is
      allowed (condition 1).
    - If the terms do allow redistribution in a source repository, a committed recorded fixture:
      - is labelled recorded, with its retrieval date;
      - carries the attribution the licence requires;
      - follows any limit the terms set on keeping or sharing the data.
    - **Key checks.** A test refuses the name of the key's query parameter or header anywhere
      under `tests/fixtures`. A count-only check against the secret file runs on the recordings
      kept outside the repository before any copy is made from them, and on the fixtures before
      any fixture commit. It prints counts only, never matches, as was done for the MET token.

### B. The "condition present" rule type (owner decision #20a; needs risk-reviewer)

14. **What it is.** A new definition kind, `ConditionDefinition` (domain, pure, frozen), and a
    new rule, `ConditionPresentRule` (risk, pure). The definition states:
    - the factor it informs (`weather`), for audit and review only; the rule never exposes it
      (decision 18);
    - the evidence field it reads (the category code of a time-resolved sample);
    - the **explicit list of category codes that count** (decision 16);
    - its scope: explicit sites and an optional maximum depth;
    - `unrepresented_conditions`: anything the signed answer depends on that the software cannot
      represent (for example dive type or qualification). A non-empty list means the definition
      is refused (ADR 0009);
    - `source`, `document_version`, preparer, reviews, read-back confirmation, sign-off date and
      a required expiry (decision 20).

    Loading, the hash pin and the refusal rules are those of ADR 0009, plus the stricter review
    rule in decision 21. Condition definitions are loaded apart from `RuleDefinition`s, so
    nothing that counts factor coverage reads them (decision 18).
15. **Which conditions count is never invented.** The code list comes only from the dive
    professional's signed answer to questionnaire B7 question 1 ("Which conditions matter"),
    which is **still unanswered** for this purpose, and from question 2 for which of them are
    no-go.
    - The answer applies per site (and optional maximum depth). An answer that depends on dive
      type is listed in `unrepresented_conditions`, so that definition is refused (ADR 0009).
    - The answer is in words. Turning the words into the provider's codes is a transcription
      and gets the ADR 0009 read-back.
    - Until it is signed, the definition does not exist and the factor stays `TBD` /
      `REQUIRES DOMAIN VALIDATION`. The repository ships no condition definition and no code
      list.
    - The owner's example (a thunderstorm category at any time in the window) is an example, not
      a definition.
16. **An explicit list of strict integers; no numbers in `risk/` (row 219, SF7).**
    - Category codes are identifiers, not thresholds, and they live only in the signed ruleset
      file.
    - A definition lists each code on its own. **No range is allowed, in the definition or in
      code**: the schema has no range form.
    - Each code must be a strict integer, not a bool and not a float, exactly as at the
      connector.
    - An empty list, a duplicate code or a value that is not a single strict integer is refused
      at load. Any refusal stops startup (ADR 0009).
    - `ConditionPresentRule` tests **membership** in the listed codes only. It does no ordering
      or arithmetic on codes, so a whole block of codes cannot be expressed at once.
    - The `risk/` numeric-literal scan stays unchanged and must keep passing.
17. **Results. It can never give GO or CAUTION (S6, S7, NS1, N3, N5).**
    - **Which items (NS1, draft 4 risk S2).** The rule reads every item whose `source` is this
      connector's. The item's `source_version` must be one of the versions given to the rule at
      construction. The source and the versions are never written as literals in `risk/`. The
      rule uses each item's series tag (decision 5).
      - An item with this source and any other version gives INSUFFICIENT EVIDENCE, or NO-GO if
        it carries a listed code. It is never ignored.
      - Items from other sources are ignored: they give no result and do not count as samples. For
        example, the data.gov.my daily forecast items are stored with
        `category=DataCategory.WEATHER` (`data/data_gov_my_forecast.py` line 215, checked on
        origin/main) and carry no condition code.
      - Within this connector's items, an item without a condition entry gives INSUFFICIENT
        EVIDENCE.
    - **NO-GO** when any sample the rule reads (in the window or bracketing it) carries a listed
      code. Any entry of the sample's condition list counts. The result cites every such item.
    - **INSUFFICIENT EVIDENCE**, with a fixed rationale, when any of these holds:
      - there is no item from this connector;
      - a required series has no sample at or before the window start, or none at or after the
        window end (for example a window past the end of the series in the data);
      - an item has no condition entry, or carries a malformed or unknown code;
      - an item lacks its recorded site id, requested point or echoed point (draft 4 risk S3);
      - an item is for another location (N3). The site's id and registered point are given to
        the pure rule at construction. The item's recorded site id and requested point must
        equal them exactly, with no distance tolerance. The rule checks only that the echo is
        present. The provider's rounding is handled by the connector's echo check (decision 5),
        never by the rule.

      **A missing category means "no data", never "no thunderstorm".**
    - **Samples are instants** (decision 5), so "brackets the window" means a sample at or
      before the start and one at or after the end. If samples become intervals, the rule reads
      every interval that overlaps the window, and no neighbours. The series then covers the
      window only when those intervals chain from start to end with no gap (draft 4 risk N1).
    - **One time model per series.** Each series has one recorded time model. An item of the
      other model gives INSUFFICIENT EVIDENCE.
    - **Two series: both are required.** The rule reads the hourly series and the 15-minute
      series. A listed code in either gives NO-GO. If either is missing, or either does not
      bracket the window, the result is INSUFFICIENT EVIDENCE.
      - Why: this choice fails closed. Reading one series would miss a code that appears only
        in the other. A rule that picked a series by what is present would let a missing series
        pass as clean.
      - Cost: a window past the end of the shorter series gets INSUFFICIENT EVIDENCE even if the
        other series reaches it. That affects availability only.
      - If the provider's 15-minute series carries no condition category (to confirm at
        implementation), it is not a series for this rule. That finding is recorded in this ADR
        and reviewed by `risk-reviewer` before build.
    - **Gaps inside a series** are detected only by the connector's per-step check (decision 5).
      Stored or re-assessed evidence has no such check: the rule sees only what the connector
      let through.
    - **No result** otherwise: every required series is present and brackets the window, and no
      sample carries a listed code. That is **not** GO. The rule says nothing, and the `weather`
      placeholder keeps the factor uncovered.
    - **Ties** go to the more severe outcome. If one sample carries a listed code and another is
      missing, the rule returns NO-GO and states the gap in its fixed rationale.
18. **It never covers a factor; a structural outcome guard (row 220, S1, N1, N6, N7).**
    - The rule never exposes a `factor` attribute. Its result carries no `factor` and no
      VALIDATED `threshold_status`, as with ADR 0031's MET check.
    - `build_engine` (`api/state.py`) never counts condition or cyclone-zone rules in `covered`,
      and the `weather` placeholder stays when a condition definition is loaded. A safety test
      checks that `placeholder.weather` is present when a condition definition is loaded and
      every sample is clean. It is mutation-checked: a mutant that adds `weather` to `covered`
      when a condition definition is loaded must turn it red.
    - The outcome guard is structural. The rule builds every result through a helper that
      accepts only NO_GO or INSUFFICIENT_EVIDENCE and raises otherwise. The engine turns a rule
      that raises into `rule.error.<id>`, which is at least INSUFFICIENT EVIDENCE
      (risk-model.md). "No result" means returning nothing; it is not a result.
    - **Fixed raise text (draft 5 security N2).** The helper and every other raise in both
      rule modules use fixed text that names no item value. The only Pydantic model either
      module builds is `RuleResult`, through the helper, from item ids and fixed text
      (draft 6 risk N4). So no validation error, which would include its input, can reach the
      engine's `logger.exception("rule raised", ...)` line (`risk/engine.py` line 356, checked
      on origin/main).
    - **Reading values (draft 6 security N4).** The rules read item values only after an
      `isinstance` check, with no conversion (such as `int()`, `float()` or `fromisoformat()`)
      and no indexing that can fail. A malformed value makes that item a gap, never an
      exception, and the rule still reads every other item (draft 7 risk N1). The ties rule
      (decision 17) applies: a listed code, or a zone containing the site, anywhere else gives
      NO-GO, with the gap stated in the fixed rationale. Otherwise the result is INSUFFICIENT
      EVIDENCE through the helper.
    - Safety tests pin the helper and the path from a raise to `rule.error.<id>`. They are
      mutation-checked: a mutant that lets the helper accept GO or CAUTION, or that removes the
      raise, must turn them red.
    - A source test checks that both rule modules (condition and cyclone zone) construct
      `RuleResult` only inside the helper, so no other code path can build a result. The test
      resolves the names each module imports, so it catches `RuleResult` under any name: its
      alias `RiskFactor` (`domain/__init__.py` line 96) or any import alias (draft 5 risk N1).
      Neither module calls `model_construct`, `model_validate`, `model_validate_json` or
      `model_copy` on it, nor the deprecated `construct`, `copy`, `parse_obj` or `parse_raw`
      (draft 4 risk N2).
    - ADR 0031 records that the `RuleResult` validator would still accept GO or CAUTION from a
      result of this shape. Tightening the validator itself is a follow-up, tracked with the
      existing `RuleResult` validator follow-up in ADR 0031's Known gaps, and is not part of this
      ADR.
19. **Tighten-only, always.** The rule runs whatever `DIVESAFE_DEGRADED_MAY_SUPPORT_GO` says,
    because it can only add severity. It cannot lower any other result: `reconcile` and the
    engine's "most severe wins" are unchanged. A human may still override a NO-GO with the
    `overrider` role and a rationale (ADR 0004, ADR 0026).
20. **Age and expiry (S2, row 222).**
    - As in ADR 0031, the rule is not given the clock. A stale item with a listed code still
      gives NO-GO, which can only tighten. A stale clean item gives no result.
    - **The rule exposes `expires_at`**, equal to the definition's expiry. The engine expires
      rules only through `expires_at`. Its `validated_without_expiry` backstop needs a VALIDATED
      status that these rules never carry, so without `expires_at` an expired definition would
      keep running.
    - The constructor refuses a definition without an expiry.
    - Safety tests pin both. An engine-level test checks that an expired definition is not run
      as a current rule, whatever condition 7 decides. If condition 7 keeps the NO-GO of an
      expired rule, that path gets its own test.
    - Today an expired definition gives `rule.expired.<id>` (ADR 0009), and the NO-GO it would
      have given is lost. ADR 0009's open question on expired rules must be **settled before
      these rule types are built** (row 222; condition 7). Whatever it decides applies to
      condition definitions and cyclone-zone definitions alike.
21. **Sign-off strength: an independent reviewer is required (row 214, N7).**
    - ADR 0009 lets one person prepare a NO-GO-only limit with a recorded reason. Here an
      omitted code silently narrows the hazard, and the read-back is the main control.
    - So a `ConditionDefinition` is `VALIDATED` only with an **independent reviewer** (not the
      preparer), even though it can only give NO-GO. A recorded reason does not replace the
      reviewer.
    - This is stricter than ADR 0009, for this kind only. The loader refuses a definition
      without an independent reviewer, and any refusal stops startup.
    - The cyclone-zone definitions (decision 25) are bound by the same rule.
    - Independence is still a casefolded name comparison (ADR 0009, Not solved), so it remains a
      process control, not something the code proves.
22. **CAUTION answers are out of scope.** If the professional names a condition as "caution",
    this rule type cannot express it, and that condition stays not evaluated. A
    CAUTION-producing condition rule would need its own ADR and review.
23. **Rule ids (N2).** `condition.<definition id>` and `cyclone_zone.<definition id>`. Neither
    is a reserved prefix (risk-model.md, "Reserved rule ids"). risk-model.md says that signed-off
    definitions always become `definition.<id>`. That sentence must be updated in the same change
    to name both new prefixes.

### C. The cyclone-zone signal (owner decision #21a; needs risk-reviewer)

24. **What it is.** A NO-GO-only rule, `CycloneZoneRule` (risk, pure), over Storm Tracker items.
    - **A new evidence category, `tropical_cyclones` (row 221).** Adding it is a domain change:
      a new member of the evidence-category enum in `domain`. The owner's answer names it
      `EvidenceCategory`; in the code today the enum is `DataCategory`. Like any change to
      `src/divesafe/domain/`, it needs `risk-reviewer` (AGENTS.md section 13).
      - It is **not** a required category (not in `INTERIM_REQUIRED_CATEGORIES`).
      - It is **not** a threshold factor (no `RiskFactorKind` is added).
      - It is not `marine_warnings`, so MET's notices stay distinct, and it gets no exemption in
        ADR 0008's (kind, category) allow-list.
    - **One item per successful read (S4).** The connector stores one `tropical_cyclones` item
      for every successful read, even when no storm is listed. Items are forecast evidence of
      DEGRADED quality, with `upstream_known=False`.
    - **What an item keeps (draft 6 security N2).** A `tropical_cyclones` item keeps a fixed
      list of keys: the storm id, the storm name, and for each zone its kind, time and validated
      vertex lists. Any other property is dropped, as for One Call items (decision 5).
      - **Storm id (draft 7 security N1).** The storm id is checked with `bounded_text`
        (`data/parsing.py` lines 40 to 48, checked on origin/main). It must not be empty, must be
        no longer than a fixed bound in `data/` and must contain only printable ASCII. A missing
        id, or anything else, rejects the feed with a fixed reason code.
      - **Grouping (draft 8 risk N1).** Zones are grouped by storm through the item's structure
        (each storm entry holds its own zones), never by matching storm ids. A storm id that
        appears twice in one read rejects the feed with a fixed reason code.
      - **Storm name.** `bounded_text` turns a missing or null name into empty text, and it is
        stored that way. That is acceptable, because names never reach a rule (decision 12).
      - **Zone kind.** The provider's zone kind is mapped to a fixed DiveSafe value, and the
        provider's kind string is never kept. An unknown kind rejects the feed. The mapping is
        a cited format table in `data/`, built from the provider's documentation at
        implementation (draft 8 risk N2). It is not a safety limit, and it is never
        one-to-many. Which DiveSafe kinds count is signed only through decision 25.
      - **Item id (draft 7 security N2).** The connector builds the item id from fixed parts
        (source, category, retrieval time and a per-read counter, or a hash computed only over
        those fixed parts), never from the storm id, the name or any other feed value (draft 8
        security N1).
    - The rule reads the provider's polygons for a storm's 34-knot wind zone and its forecast
      cone, with their times.
    - Which specialist agent, if any, receives these items is an `ai-engineer` question at build
      time. An agent never decides from them (option 4), and the prompt allow-list of
      decision 12 applies.
25. **Which zones count is signed, not invented.** The owner's examples are: the site lies
    inside the 34-knot zone, or inside the cone during the dive window.
    - Whether these apply comes through the same signed route as decision 15: questionnaire B7
      (tropical cyclones are a weather condition there) and an ADR 0009-style definition.
    - The answer applies per site (and optional maximum depth). An answer that depends on dive
      type goes in `unrepresented_conditions`, so that definition is refused.
    - The definition needs an **independent reviewer** (decision 21) and an expiry
      (decision 28).
    - Until it is signed: TBD / REQUIRES DOMAIN VALIDATION, and nothing is active.
26. **Results. It can never give GO or CAUTION (NS2, N8).**
    - **NO-GO** when the site's registered point is inside, or on the boundary of, a signed zone
      kind whose time overlaps or brackets the dive window. The result cites the storm items.
    - **INSUFFICIENT EVIDENCE** in these cases:
      - there is no `tropical_cyclones` item, because the feed failed or was not read
        (decision 24). `tropical_cyclones` is not required, so the engine's sufficiency check
        does not catch a missing feed; the rule itself must.
      - a listed storm's latest zone time, read from the data, is before the window end, or its
        earliest zone time is after the window start (draft 4 risk N3). The feed then does not
        say where that storm is for part of the window;
      - the basin does not cover the site, or no basin boundary has been accepted (below);
      - an item reaches the rule with geometry that is not valid. The connector should have
        rejected it (decision 27); this is a backstop.
    - **No result** when the item holds a valid empty collection (no storm listed), or when no
      signed zone contains the site and every listed storm's zone times span the window. "No
      cyclone listed" is like `no_active_warnings`: it is not proof of calm and never supports
      GO.
    - **Basin (N5, N8; draft 4 risk N5).** The boundary used is the provider's documented one,
      cited and reviewed. It is held as data in `domain`, or in a hash-pinned file like the
      ruleset, and is given to the pure rule at construction. It is never read from a feed
      item, never written in `risk/` and never an invented box.
      - It is used only once the owner accepts a cited boundary after risk review
        (condition 10).
      - While none is accepted, every assessment gets INSUFFICIENT EVIDENCE from this rule. So
        the rule is not switched on until a boundary is accepted.
      - The boundary falls under the same geometry checks as decision 27, including the
        antimeridian refusal. A boundary with an edge on longitude 180 or -180 does not cross
        the antimeridian; a boundary that wraps across it does. A documented boundary that
        crosses it cannot be used as written. The owner decides how it is handled, after risk
        review, and the rule stays off meanwhile (draft 4 risk N4).
27. **Geometry (SF8, N4, N6, N9).**
    - The connector validates the feed with Pydantic before any item exists:
      - finite coordinates, latitude from -90 to 90 and longitude from -180 to 180;
      - no non-finite number in any other value kept from the feed (draft 4 security N2);
      - closed rings with at least four positions;
      - fixed maximum numbers of features and rings, of vertices per ring, and of vertices
        overall. These are input-size limits in `data/` or a domain validator, never in
        `risk/`, and they are not safety limits;
      - every zone has a time, and every time is timezone-aware. A missing zone time rejects the
        feed;
      - self-intersecting rings and holes are refused, unless `risk-reviewer` defines how they
        are read;
      - a polygon crossing the antimeridian is refused, never guessed.

      Anything else rejects the whole feed, with a fixed reason code in the issue (decision 9).
    - **Empty versus failed.** "No storm" must be a valid empty collection. A missing or
      malformed collection is a feed failure: no item is stored and a connector issue is
      recorded.
    - **The rule's test.** A point-in-polygon test over the validated vertex lists. No buffer,
      distance tolerance or interpolation between track points or zone times is added, because
      each would need a number. A site just outside a polygon gets no result (see "What this
      does NOT solve").
28. **It never covers a factor; guard and expiry (row 220, S1, S2, N1).** The same as decisions
    18 and 20:
    - no `factor` attribute, and the rule is never counted in `covered` by `build_engine`;
    - the structural result helper that accepts only NO_GO or INSUFFICIENT_EVIDENCE, with the
      same mutation-checked tests and the same source test;
    - `expires_at` equal to the definition's expiry, and a constructor that refuses a definition
      without one.

    Its rule id is `cyclone_zone.<definition id>` (decision 23). The `RuleResult` validator
    follow-up applies here too. The rule does not touch the `forecast_uncertainty` factor.

## Mapping against the existing design

- **ADR 0008 (evidence usability):**
  - items are DEGRADED forecast or model data, never observations, and the engine judges
    `effective_quality`;
  - decision 3 binds `weather` to `risk.DEGRADED_NEVER_SUPPORTS_GO` in the code change that adds
    a weather metric, from any source;
  - the hold acts only through `usable`, which comes from the required categories (checked on
    origin/main), so the citation guard is the preferred route. It is category-wide (it also
    covers `tides`: harmless while `tides` is required, and only tightening otherwise). It
    checks a result that cites a held category or whose factor reads it, through the shared
    factor-to-categories constant or the definition's metric, both read from the rule object.
    It also checks that VALIDATED citations cover the window, and it emits the reserved engine id
    `evidence.quality_held_category.<rule id>`;
  - a daily item never stands in for hourly data (`COARSE_SPAN`);
  - samples are stored as instants; a gap between instants is caught only by the connector;
  - rules still receive all evidence, so the hazard rules see stale items;
  - `tropical_cyclones` gets no exemption.
- **ADR 0009 (rule definitions):** the same loading, refusal, hash pin and read-back. New for
  both new kinds:
  - a set-membership definition with a single outcome, NO-GO;
  - an explicit list of strict integers;
  - `unrepresented_conditions`;
  - a required expiry exposed as `expires_at`;
  - an independent reviewer.

  ADR 0009's open question on expired rules is settled before build. `weather` stays without a
  numeric metric, so the coverage guard keeps GO blocked, and the invariant test of decision 3
  keeps a future metric tied to the hold.
- **ADR 0031 (MET tighten-only):** the same shape: no factor, no status, tighten-only, always on,
  no clock. Decisions 18 and 28 add structural outcome guards, a source test and `expires_at`.
  The `RuleResult` validator follow-up stays in ADR 0031's Known gaps.
- **ADR 0005 (warnings):** unchanged. MET stays the warning source, and the provider's alerts
  are not used for Malaysia.
- **ADR 0010 (lineage):** `upstream_known=False`, so the source never corroborates another.
- **ADR 0024 (deployment):** "What leaves the machine" and the key rotation step are updated in
  the switch-on change (decisions 9 and 11).
- **AGENTS.md section 12:** gains the rule that no agent creates accounts, keys or subscriptions
  or accepts terms (row 230), in the same docs change as this ADR.
- **Domain (`src/divesafe/domain/`):** a new evidence category `tropical_cyclones`, a new
  `ConditionDefinition` and a cyclone-zone definition kind. All need `risk-reviewer`.
- **Factor-to-categories map (`risk/placeholders.py` line 30):** `_FACTOR_CATEGORIES` maps each
  factor to a set of categories. It moves from a private name to one shared, reviewed constant,
  read by the placeholders and by the citation guard (decision 3). Needs `risk-reviewer`.
- **`api/state.py`:** `build_engine` keeps the new rules out of `covered` (decision 18).
- **`data/http.py`:** a new credentialed getter that checks paths exactly and raises errors
  with no chain (decisions 8 and 9). `check_url` and the shared getter are not changed by this
  ADR; any change to them needs its own review.
- **Connector error text:** these connectors carry fixed reason codes, never the `str()` of a
  parse error, unlike the existing `unusable response: {clip(exc)}` pattern (decision 9). This
  ADR does not change the existing connectors.
- **`data/ratelimit.py`:** the cache and budget layer raises with fixed text after its `except`
  block has ended (decision 9). This change is part of this ADR's build, under its own review.
- **Prompt layer (`agents/prompts.py`):** `tropical_cyclones` gets its own envelope, built like
  `_knowledge_item`, and never takes the generic branch of `_payload_item` (decision 12). Needs
  `ai-engineer` and `security-reviewer`.
- **data-sources.md:**
  - a **Candidate** row for both products;
  - the connector contract and the HTTP hardening;
  - a credentialed getter bound to a host and an exact path list;
  - a secret file with a shape check, and a budget;
  - recorded responses kept outside the repository, with a SYNTHETIC committed fixture, as for
    tides.today.

  Host allow-list: `api.openweathermap.org`, plus Storm Tracker's host if the owner approves it.
- **risk-model.md:** two new non-numeric, NO-GO-only rules under "Non-numeric policy rules"; the
  reserved-id sentence updated for `condition.` and `cyclone_zone.`; the `weather` placeholder
  unchanged; new Known gaps entries (below). Its "off by default" sentence needs no change.
- **questionnaire.md:** B7 questions 1 and 2 are the source of the code list and the zone kinds,
  per site (and optional maximum depth). B7 "Data we hold" changes only once a connector exists.
  Part C (visibility) is unchanged, because One Call visibility is air visibility.
- **AGENTS.md sections 5 and 7:** tighten-only, fails closed, no thresholds in `risk/`, and a
  missing field means "no data".

## What this does NOT solve

- **Tidal current.** Neither product gives tidal streams. `tidal_current` stays without a
  metric, and `tests/safety/test_tide_is_not_a_current.py` is unchanged.
- **Site constraints.** Neither product has exposure, entry and exit, reef-scale effects or
  local rules. `site_constraint` stays unencodable.
- **General forecast uncertainty.** The cyclone cone is track spread for cyclones only. One Call
  gives no ensemble spread or provider uncertainty for its values. `forecast_uncertainty` stays
  unencodable.
- **Ordinary thunderstorms in Storm Tracker.** Only the One Call category rule (if signed) can
  see them, and only as the provider's forecast category, not as a lightning or radar
  observation.
- **Underwater visibility, gusts and rain amount.** Air visibility is not dive visibility. No
  gust rule exists. `rain.1h` and `pop` have no definition, and none is proposed here.
- **One Call wind.** It is out of this study and needs a separate ADR (decision 4).
- **Dive type.** A signed answer that depends on dive type is refused, not encoded.
- **Weather as an evaluated factor.** A clean condition check gives no result, so `weather`
  stays not evaluated. That keeps GO unreachable today, as do the placeholders of the three
  other unencodable factors.
- **Resolution and lead time.** The producing model, grid and run time are not stated. A local
  storm cell smaller than the model's grid may not appear at all. Forecast skill that varies
  with lead time is not modelled.
- **Near-miss geometry.** A site just outside a cyclone polygon gets no result. A margin needs a
  number from a professional.
- **Between zone times.** The provider draws 34-knot zones at separate times. A storm can cross
  a site between two of them, with the site in neither zone. A signed cone may cover it; the
  rule does not interpolate between times.
- **Storms that form later.** "No storm listed" says nothing about a storm that forms between
  the read and a later dive.
- **Malaysian national alerts.** This source does not provide them.

## Licence and price check (2026-11-23, owner decisions #57a to #59b)

Condition 1, checked by reading the provider's public pricing page, full price table, FAQ,
One Call 4.0 and Storm Tracker documentation, its terms and conditions of sale and its website
terms of use, all on 2026-11-23. Nothing below is legal advice; the owner reads the terms before
registering. Vendor text is paraphrased, not copied.

- **Price (One Call 4.0).** A separate pay-per-call subscription. The first 1,000 calls a day
  are free; each further call costs 0.0012 GBP, VAT not included, billed at the end of the
  subscription month. Each extra page of a response is a separate call.
- **Account cap.** A new One Call subscription has a daily cap of 2,000 calls, which the owner
  can change. Caps are per account, summed across all of its keys. Over the cap the provider
  answers 429 and blocks the account until the next daily reset (00:00 to 00:05 UTC); raising
  the cap does not lift a block early. A block is a failed source for every site until the
  reset, so it gives INSUFFICIENT EVIDENCE, at a cost to availability. The sum of the budgets
  of every process that can call, over all restarts in one provider day (00:00 UTC), counting
  every page as a call, plus a reserve for the owner's manual calls, stays below the account
  cap (condition 4). This holds either by a count kept across restarts, or by a bounded restart
  policy whose worst case the owner records (condition 4). If the count or the restart bound is
  reached, the connector stops calling for the rest of the provider day (a used-up budget, which
  fails closed); it never relies on the provider's 429 as the limit. The connector's budget day
  is the provider's UTC day. At switch-on the account holds only the connector's key (#62a), and
  no other project uses it. On a 429 the connector makes no further call until 00:05 UTC. The
  budget is never met by lengthening the cache TTL or by serving older items.
- **Worst-case cost.** At the default cap of 2,000 calls, at most 1,000 calls a day are
  billable: 1.20 GBP a day before VAT, about 36 GBP a month. With the cap at or below the free
  allowance (decision 10), the worst case is nil. Raising the cap needs the owner to record the
  new worst case (condition 4).
- **Commercial use.** Allowed on every self-service plan. DiveSafe's shared `USER_AGENT` says
  non-commercial research; that is no conflict with this provider. Whether the wording stays
  accurate is a question about DiveSafe, not about this source.
- **Licence.** The pricing pages put every self-service plan under ODbL 1.0. Share-alike applies
  only when a database built from the provider's data is made available outside the
  organisation. Whether records, backups and the API's record views are used only inside the
  organisation depends on who holds API keys and where backups are kept. The owner confirms
  both when registering; until then, no share-alike conclusion is drawn. The terms of sale
  also name CC BY-SA 4.0 for the provider's "products and services" alongside ODbL for the
  data. Where the two documents disagree, the stricter reading applies until the owner
  resolves it.
- **Keeping data.** Nothing found forbids keeping provider values in records or backups. No
  clause was found in the terms of sale or the website terms that requires deleting kept data
  when the subscription ends. Records are write-once, so such a duty would clash with the audit
  design; it is rechecked if the terms change.
- **Redistribution in a source repository.** A committed recorded fixture would be a copy of
  the data outside the running system, with ODbL duties. Row 229 stands: the committed fixture
  stays SYNTHETIC, and recorded responses stay outside the repository.
- **Sending values to the model host.** The terms do not address it. A hosted model host is a
  third party (ADR 0024, "What leaves the machine"), so this is not assumed to be internal use.
  The owner decides it at registration, as part of the licence decision (Outcome, item b), and
  it is recorded in data-sources.md. With a declared-local model server, it is treated as
  internal use, on the operator's declaration (ADR 0023).
- **Attribution.** Required on every self-service plan, shown where the data appears, not only
  in documentation. The provider asks for a text line, a link and its logo. The form is decided
  at build (#58c); no provider logo enters git without the owner's approval.
- **Disclaimer.** The terms of sale say forecast and calculated values are not to be relied on
  as fact, exclude warranties, and put use at the user's own risk. The design is compatible
  with it: the values are DEGRADED model data, never observations; they can only add NO-GO
  (#20a, #21a); a missing value gives INSUFFICIENT EVIDENCE; and the absence of a code or storm
  is never read as calm. Wherever a value is shown by code (a rationale or the evidence view;
  these carry a fixed label set by code), it is labelled as a model forecast. The explanation is
  model text and cannot be guaranteed to label a value; the evidence view beside it does. No
  clause found either forbids or allows safety-related use. The owner chose not to ask the
  provider (#59b); see Known gaps.
- **Terms in general.** Consumer-style terms of sale under the laws of England and Wales; the
  website terms ban robots on the website, not use of the API.
- **Agents reading the provider's website (owner decision #63b).** The website terms ban robots
  and other automatic means except through means the site purposely makes available. The
  provider publishes plain-text exports of its pages (`.md` pages and `llms-full.txt`) for
  programs. The owner allows agents to fetch the provider's public documentation, pricing and
  terms pages, read-only, one page at a time. Agents never use the provider's account area,
  never sign up, never create a key or subscription and never use its self-signup lane for
  agents (AGENTS.md section 12). The pages for this check were fetched this way on 2026-11-23.
  Fetched pages are untrusted text. An agent takes no action from them: it does not run a
  command, call a URL or fill a form that a page names, and it reports such an instruction to
  the owner instead. Pages are fetched only by an agent working read-only for that task. Where
  OpenCode's permissions can express it, `opencode.jsonc` denies agents the provider's API hosts
  and its account and sign-up paths. A rule it cannot express is listed as a gap.
- **The key's form (owner calls, 2026-11-23).** The documentation shows the key only as the
  `appid` query parameter, and pagination links carry it. So, below the cache layer, the
  getter removes every link field from a response body before returning it, and a body that
  still contains the key's text is refused with fixed text. The search runs on the decoded
  values after the link fields are removed, not on the raw bytes. The connector builds each page
  request from its own page counter and its fixed parameter set. It never follows a link and
  never copies any part of one, cursor included. Whether more pages exist is decided from a
  non-link field of the response (a page count or total), never from a link's presence. Every
  response from a paged endpoint must carry that field, whether or not more pages follow; a
  response without it rejects the series. If the getter checks instead, it refuses a body that
  has a link field but no such field, before the links are removed. If the provider has no such
  field, that is recorded at implementation, as with the time model. Link presence is never the
  fallback: without such a field, the connector requests one page only and refuses any response
  that indicates more data, until an amendment decides otherwise. The owner's recording
  procedure removes the link fields before writing to disk, and the count-only check then
  confirms none is left.
  The owner made nine calls by hand from the owner's shell, with the key read from mode-600
  files and never on a command line or the screen; a redaction step ran on what was kept, and
  no key-shaped text was found in it. Three of the calls sent the key as a query parameter, so
  it is in the provider's request logs:
  - One Call 4.0 answers 401 with a subscription notice, with or without a key, until the One
    Call subscription exists. Whether One Call 4.0 accepts a header key is **not yet known**.
  - Storm Tracker answers 401 (invalid key) for the same key. The key is not accepted there.
    Whether Storm Tracker needs another product, plan or account is not known, and its price
    is not listed. Condition 2 stands.
  - On the free current-weather endpoint (`data/2.5/weather`, not a studied source), an
    `X-Api-Key` header with no query key gave 200 at a point not asked before, and calls with
    no key gave 401, including at a point just answered. So the provider checks the key before
    its cache, and the header form works there.
  - Decision 9 therefore stays as written: a header if the studied endpoints accept one, which
    is checked once the owner subscribes, before any connector is written. The check uses the
    same method as calls 8 and 9: a header-only call at a point not asked before, and a no-key
    call at the same point, both from the owner's shell.
- **Outcome.** The check found no term that meets the "not registered if" test of condition 1.
  Condition 1 stays **open** until the owner records, at registration: (a) the reading of the
  CC BY-SA 4.0 and ODbL disagreement; (b) that sending values to the model host is use inside
  DiveSafe (or that it is not, and the values are then not sent); (c) acceptance of the Known
  gap on safety-related use; and (d) taking the One Call subscription, which means accepting
  the terms of sale (#60). Registering stays the owner's decision (#19a). If (b) is "not", the
  build keeps these items out of every prompt under every hosted egress basis. This is recorded
  like `withheld_knowledge` and tested before switch-on. The items stay in the evidence that the
  rules read and in the record; only the prompt omits them. A test checks that a listed code
  still gives NO-GO while the items are kept out of the prompt.

## Conditions before acceptance, build or switch-on (all unresolved)

1. **Licence, terms and price, then registration (owner; row 212; SF5, N4).** Registration is
   decided only after this check. The check covers:
   - commercial use (DiveSafe's other sources are non-commercial today);
   - keeping provider values in stored assessment records and backups, and keeping recorded
     responses outside the repository;
   - redistribution in a source repository (decides whether a committed fixture may be
     recorded);
   - sending values to the model host;
   - any attribution wording, and where it must be shown (the `attributions` field and
     fixtures);
   - the ODbL mention on the provider's agent page: what it covers, and whether share-alike or
     attribution duties attach to stored records;
   - any safety or "not for life-safety use" clause, like the one found for WorldTides;
   - the per-call price, the free daily allowance, how pages are counted, and any account cap;
   - the shared connector `USER_AGENT`, which says non-commercial research, settled against a
     commercial account before any call.

   **The source is not registered if** the terms forbid keeping provider values in records or
   backups, or if share-alike or publication duties would attach to records, backups or
   fixtures. Stored records cannot drop evidence, and they cannot be published.
2. **Storm Tracker host and path (owner; row 215):** once the host and path are known, the owner
   decides whether they are allowed (decision 8). Until then Storm Tracker is not built.
3. **Fixtures (rows 213 and 229):** recorded responses stay outside the repository. Only the
   body of a successful 2xx response is recorded, by the owner, with the key read from the
   secret file. The committed fixture is SYNTHETIC unless the terms explicitly allow
   redistribution in a source repository. Nothing is recorded until condition 1 confirms that
   storing provider data is allowed (decision 13).
4. **Budget across processes (owner; SF3, draft 3 security SF2):** the budget is per process
   and not shared, and an overrun costs money. Before switch-on, the owner sets the allowance
   divided by the largest possible process count with a margin, and sets any account cap at or
   below the free allowance (decision 10). **If the provider offers no account cap**, the source
   is not switched on until either:
   - the count persists across restarts (for example in DiveSafe's PostgreSQL, counted before
     the call), or
   - the owner records the worst-case cost of a restart loop and accepts it.
5. **Questionnaire (dive professional):** a signed answer to B7 questions 1 and 2 for this
   purpose. It says which conditions count as NO-GO and whether the cyclone zones count, per site
   (and optional maximum depth). Then the read-back of the mapping from words to codes, and an
   independent reviewer (decision 21). An answer that depends on dive type is refused.
6. **Reviewer routing (draft 9 confirmed; any later change needs both reviewers):**
   - `security-reviewer`: the credentialed getter, its exact path check and chain-free errors,
     the host and path lists, key handling and the canary-key test, the request parameter set,
     the location echo, geometry and size bounds, fixed reason codes in issues, the cyclone
     prompt envelope, the kept-key lists of One Call and Storm Tracker items, fixed units,
     fixed raise text and type-checked reads in the rule modules, the `ratelimit.py` raise,
     storm-name, storm-id and zone-kind handling, item ids built from fixed parts, recorded
     fixtures outside the repository,
     the budget and egress;
   - `risk-reviewer`: `ConditionDefinition`, `ConditionPresentRule`, `CycloneZoneRule`, the
     `tropical_cyclones` category, the structural outcome guards and source test, `expires_at`,
     the `placeholder.weather` test, the item selection by source and version, the two-series
     rule, the time model, the location rule, the storm-time and basin rules, the binding
     `weather` hold, the citation-guard route with its scope, coverage check and result id,
     the shared factor-to-categories constant and its agreement with `FACTOR_METRICS`
     (decision 3), and the mapping above.

   Both review the design before any connector code is written, and the code before it is
   merged.
7. **Expired rules (owner and `risk-reviewer`; row 222):** ADR 0009's open question on expired
   rules is settled **before** `ConditionPresentRule` or `CycloneZoneRule` is built.
8. **AGENTS.md section 12 (row 230):** the sentence of decision 9 is added in the same docs
   change as this ADR.
9. **ADR 0024 at switch-on:** "What leaves the machine" (values to the model host, coordinates
   to OpenWeather) and the key rotation step are recorded in the change that switches the source
   on (decisions 9 and 11).
10. **Basin boundary (owner after `risk-reviewer`; N8):** the cyclone rule is not switched on
    until the owner accepts a cited, documented basin boundary after risk review
    (decision 26).

## Owner decisions (2026-11-23)

1. **Registration** (row 212): decided only after the licence and price check. This is
   condition 1, not an open question.
2. **Recorded fixtures** (row 213, narrowed by row 229): allowed, by the owner's choice over the
   synthetic-only recommendation, once the licence check confirms that storage is allowed
   (decision 13, condition 3).
3. **Independent reviewer** (row 214): required for condition definitions even though they are
   NO-GO-only, and for cyclone-zone definitions. Stricter than ADR 0009 for these kinds
   (decision 21).
4. **Storm Tracker host** (row 215): decided once its host is known; not refused in advance
   (decision 8, condition 2).
5. **One Call wind and gust** (row 216): may enter `wind` later, only through a separate ADR that
   solves the second source, the undetected disagreement with Open-Meteo and the missing gust
   rule. Out of this study (decision 4).
6. **`weather` hold** (row 217): before any numeric `weather` definition, `weather` joins
   `risk.DEGRADED_NEVER_SUPPORTS_GO`. Binding, and broadened after risk review to definitions
   from any source (decision 3).
7. **`weather` not required now** (row 218): decided with the first numeric weather limit. The
   stale-item Known gaps stay.
8. **Explicit code list only** (row 219): no ranges, in the definition or in code (decision 16).
9. **Outcome guard** (row 220): a per-rule-type guard with a mutation-checked safety test now,
   made structural after risk review. Tightening the `RuleResult` validator is a follow-up,
   linked to the existing `RuleResult` validator follow-up (decisions 18 and 28).
10. **`tropical_cyclones`** (row 221): a new evidence category for Storm Tracker items. It is a
    domain change that needs risk review, and it is neither required nor a threshold factor
    (decision 24).
11. **Expired rules first** (row 222): ADR 0009's open question is settled before these rule
    types are built (decision 20, condition 7).
12. **Recorded responses outside the repository** (row 229, #45a, narrows row 213):
    - the committed fixture stays SYNTHETIC unless the terms explicitly allow redistribution in
      a source repository;
    - only the body of a successful 2xx response is recorded. The owner records it from the
      owner's shell, never an agent, with the key read from the secret file and never typed on
      the command line;
    - a test refuses the key parameter's or header's name under `tests/fixtures`;
    - a count-only check against the secret file runs on the recordings kept outside the
      repository, and before any fixture commit (decision 13).
13. **No agent sign-up or acceptance of terms** (row 230, #46a): AGENTS.md section 12 gains the
    rule, in the same docs change as this ADR (decision 9, condition 8).

## Tests (when built; safety, mutation-checked)

- **Condition rule outcomes:**
  - a listed code in the window, or in a bracketing sample, of either series gives NO-GO;
  - an unlisted code gives no result;
  - the result helper accepts only NO_GO or INSUFFICIENT_EVIDENCE and raises otherwise, and the
    engine turns the raise into `rule.error.<id>`. A mutant that widens the helper or removes the
    raise must fail;
  - a source test checks that both rule modules construct `RuleResult` only inside the helper,
    under any name (`RiskFactor` or an import alias, resolved from each module's imports). It
    also checks that neither module calls `model_construct`, `model_validate`,
    `model_validate_json`, `model_copy`, `construct`, `copy`, `parse_obj` or `parse_raw` on it;
  - every raise in both rule modules has fixed text: given an item holding a sentinel value,
    or a sentinel in an item id, neither the exception nor the engine's "rule raised" log
    record contains the sentinel (draft 5 security N2, draft 6 risk N4);
  - the same case runs with malformed items (a code as a string, a point as a string, a missing
    key). Alone, each gives INSUFFICIENT EVIDENCE through the helper, with no exception and no
    sentinel in any log record (draft 6 security N4);
  - a malformed item beside a well-formed item carrying a listed code gives NO-GO, in either
    order. The same holds for a malformed storm beside a storm whose zone contains the site. A
    mutant that returns on the first malformed item must fail (draft 7 risk N1).
- **Item selection (NS1):**
  - a data.gov.my daily `weather` item does not change the outcome, alone or beside One Call
    items;
  - a One Call item without a condition entry still gives INSUFFICIENT EVIDENCE.
  - an item with this connector's source and a version not given at construction gives
    INSUFFICIENT EVIDENCE, or NO-GO if it carries a listed code;
  - an item missing its site id, requested point or echoed point gives INSUFFICIENT EVIDENCE;
  - an item whose recorded time model differs from its series' model gives INSUFFICIENT
    EVIDENCE, in both directions.
- **INSUFFICIENT EVIDENCE cases:**
  - no item from this connector, or a missing series of the two required;
  - no sample at or before the window start, or none at or after the window end;
  - a missing condition entry, or an unknown code;
  - a recorded site id or requested point that does not exactly match the site.

  An absent `rain.1h` is never read as zero; this is pinned if any rule later reads it.
- **A NO-GO is kept** when another sample or series is missing, and the gap is stated in the
  fixed rationale.
- **The factor stays uncovered:** `placeholder.weather` is present when a condition definition
  is loaded and every sample is clean. A mutant that adds `weather` to `covered` when a condition
  definition is loaded must fail. The coverage guard still blocks GO.
- **Expiry:**
  - the rule's `expires_at` equals the definition's expiry;
  - the constructor refuses a definition without one;
  - an engine-level test shows that an expired definition is not run as a current rule;
  - the path that condition 7 decides gets its own test.

  The same tests apply to the cyclone-zone rule.
- **Loader:**
  - refuses a condition or cyclone-zone definition without an independent reviewer, including
    one that has a recorded reason instead;
  - refuses a non-empty `unrepresented_conditions` (for example a dive-type dependency);
  - refuses an empty code list, a duplicate code, a range, and any value that is not a strict
    integer (a bool, a float or a numeric string).
- **No numbers in `risk/`:** the repository holds no definition, and the `risk/` numeric-literal
  scan passes with no codes and no geometry bounds in `risk/`.
- **The `weather` hold (decision 3, N1):**
  - an invariant test: if `WEATHER` is in `FACTOR_METRICS`, then `WEATHER` is in
    `DEGRADED_NEVER_SUPPORTS_GO`. A mutant that adds the metric alone must fail it;
  - an invariant test: for every factor and every metric in `FACTOR_METRICS`,
    `SUPPORTED_METRICS[metric].category` is in the shared map's set for that factor. They agree
    today. A mutant that maps a metric's factor elsewhere must fail it (draft 6 risk N2);
  - engine tests, each run with the setting on and again with it off, with `weather` not
    required. A weather GO result gives INSUFFICIENT EVIDENCE with the result id
    `evidence.quality_held_category.<rule id>` if it cites DEGRADED weather evidence, or if it
    cites a VALIDATED weather item outside the window while DEGRADED items cover the window;
  - a weather GO that cites no weather item gives the same result id. A mutant that drops the
    "must cite at least one item" bullet must fail this test;
  - a GO on another factor that cites nothing in a held category is not flagged for that
    category: with `tides` held, a wave-height GO is unchanged by the route. A mutant that
    drops the "is checked when" scoping must fail this test;
  - that result's fixed rationale appears under "Evidence problems" in the outcome note. A
    mutant that renames the id outside `ENGINE_RULE_PREFIXES` must fail;
  - with `tides` required and the setting off, a test rule's GO that cites a DEGRADED tide item
    gives the same overall outcome with and without the route (draft 6 risk N3).
- **`tropical_cyclones`:** it is not in the production required categories and maps to no
  threshold factor.
- **Cyclone rule:**
  - inside the zone, or on its boundary, gives NO-GO;
  - outside it, or a valid empty collection, gives no result;
  - no item, or a basin that does not cover the site, gives INSUFFICIENT EVIDENCE;
  - a listed storm whose latest zone time is before the window end, or whose earliest zone time
    is after the window start, gives INSUFFICIENT EVIDENCE (NS2, draft 4 risk N3);
  - without an accepted basin boundary, every site gives INSUFFICIENT EVIDENCE.
- **Cyclone connector, which rejects the whole feed for:**
  - a non-finite or out-of-range coordinate;
  - a non-finite number in any other kept value;
  - a storm id that is missing, empty, not a string, over-length or not printable ASCII;
  - a storm id that appears twice in one read (draft 8 risk N1);
  - an unknown zone kind;
  - an unclosed ring, or one with fewer than four positions;
  - more features or rings, more vertices in one ring, or more vertices overall than the bounds
    allow;
  - a missing zone time, or a zone time without a timezone;
  - a self-intersecting ring, a hole, or a ring crossing the antimeridian;
  - a missing or malformed collection, which gives no item and records an issue.

  One item is stored for every successful read, including one with no storm. The storm name is
  checked as a string with `bounded_text`, passed through `clip()` with a fixed length, kept as
  text and never reaches a rule. A `name` that is a list or an object rejects the feed with a
  fixed reason code (draft 6 security N1). A missing name is stored as empty text. A sentinel
  under an unknown property appears in no item (draft 6 security N2). A sentinel storm id
  appears in no item id and no prompt, and no item holds the provider's zone kind string
  (draft 7 security N1, N2). With a fixed clock, two reads that differ only in their storm ids
  and names give the same item ids (draft 9 security N1).
- **Fixed reason codes (draft 4 security SF1):** for each rejection, by the cyclone connector
  and by the One Call connector's value checks, the issue text equals the fixed reason code. A
  sentinel coordinate and a sentinel storm name in a rejected payload appear in no issue, no
  rule rationale and no prompt.
- **Cyclone prompt envelope (security N4, draft 4 security SF2):** a sentinel vertex in a
  polygon, and a sentinel value under an unknown extra key, appear in no prompt built by
  `evidence_payload`, the specialists, the scenarios or the risk proposal. A mutant that sends
  `tropical_cyclones` through the generic branch of `_payload_item` must fail.
- **Connector, requests:**
  - only the fixed parameter set is sent, and a test pins it;
  - no plan text, site name, dive time, actor, key id or request id is sent, in the query or in
    a header;
  - no item holds the request URL, query or headers in any field;
  - other hosts are refused, and redirects are refused;
  - the path is compared exactly: a prefix, a percent-encoded or normalised variant, a trailing
    slash or a change of case is refused before connecting;
  - a URL with a query, a fragment or a `;` parameter is refused, and so is a `params` entry
    named like the key parameter, in any letter case;
  - page URLs are never taken from the response, and reaching the page bound rejects the series.
- **Connector, values:**
  - an unknown, non-integer or over-length condition list rejects the series;
  - a `NaN` or infinite value anywhere in a kept value rejects the series;
  - a missing location echo, or one that does not match the request after the documented
    rounding (or exactly, if none is documented), rejects the series.
  - a sentinel string under an unknown key of a One Call sample, and one in a condition's
    description field, appear in no item and no prompt (draft 5 security N1).
  - a response unit that differs from the connector's fixed unit for that field rejects the
    series, and no item holds the provider's unit string (draft 6 security N3).
- **Connector, budget:** calls are counted before they are made, and failed and refused calls
  count. A used-up budget fails closed.
- **Connector, pagination links (security SF1):** a paged 2xx body whose links carry the canary
  key: the key appears in no cached value, no item, no issue and no log record. A page request
  holds no value taken from the response.
- **Connector, account block and lapsed subscription:** a 429 (account block) and a 401
  subscription notice each give a typed connector failure, never a valid empty series. The
  assessment records a connector issue, the condition rule gives INSUFFICIENT EVIDENCE ("no
  item from this connector"), and no cached item older than the TTL is used. A mutant that
  treats either answer as an empty series must fail. The same for the Storm Tracker connector:
  a 429 or a 401 gives a typed connector failure and no item, never a valid empty collection,
  and the cyclone rule gives INSUFFICIENT EVIDENCE ("no `tropical_cyclones` item"). A mutant
  that stores an empty collection for either answer must fail.
- **Canary key (B1).** A unique sentinel key is run through every path:
  - 401, a 3xx response and 429;
  - a connection error (`URLError`) and an `http.client.HTTPException`;
  - a timeout;
  - an over-cap body, bad JSON and deep nesting;
  - cancellation;
  - a successful call.

  Then:
  - every exception leaving the getter, the cache and budget layer (`data/ratelimit.py`) or the
    connector has `__cause__ is None` and `__context__ is None`. The assertion is never
    loosened; if a layer is not covered, the test names the layers it covers;
  - no reachable exception holds the sentinel in its `args` or `vars()`, nor, for `HTTPError`
    and `URLError`, in `url`, `filename`, `reason` or `headers`;
  - the sentinel appears in no `str()` or `repr()` of any exception, no formatted log record
    (including `exc_info`), no issue, no stored record JSON and no `repr()` of the cache, and
    not in the cache key or the cached response value;
  - after the successful call, it appears nowhere in the item, its provenance or the stored
    record.

  Issues carry only fixed text, the HTTP status and, for a rejected payload, a fixed reason
  code. A source test checks that the getter and the connector are never called from inside an
  `except` or `finally` block.
- **Key setting:** the shape validator refuses a malformed key, with fixed text that never echoes
  the key.
- **Fixtures:** a test refuses the key parameter's or header's name anywhere under
  `tests/fixtures`.
- **Alerts:** the alerts section never produces a "no warnings" item.

## Known gaps (of the design, if built)

- **An omitted code** is not checked, and nothing reports it. The definition's author declares
  the code list and `unrepresented_conditions`. The read-back and the independent reviewer are
  the only controls, and independence is a casefolded name comparison (ADR 0009).
- **The condition rule has no clock**, so a stale clean item gives no result instead of
  INSUFFICIENT EVIDENCE (decision 20). `weather` is not required (row 218), so the engine's
  freshness check does not catch it either. This is acceptable only while the `weather`
  placeholder (and the other unencodable factors' placeholders) block GO, and it is decided
  again with the first numeric weather limit.
- **A stale cyclone feed (S4).** A stale feed, or a stale "no storm" item, gives no result. Only
  the placeholders block GO meanwhile. Whether `tropical_cyclones` becomes required, or the rule
  gets a freshness input, is decided with the first numeric weather limit (row 218).
- **Nothing visible when clean (N6).** A clean or stale condition check, or a cyclone check with
  no storm, leaves no rule result in the record. So a human cannot see that the check ran or what
  it read. The evidence items are in the record; the rule's silence is not.
- **The per-category hold needs a route.** `DEGRADED_NEVER_SUPPORTS_GO` acts only over `usable`,
  which comes from the required categories (checked on origin/main). Decision 3 makes the
  citation-guard route, or making `weather` required, a precondition of the first weather
  metric. Until a weather metric exists, the loader's refusal and the invariant test are the
  enforcement.
- **The `RuleResult` validator** still accepts GO or CAUTION from a result with no factor and no
  status. Only the per-rule-type structural guards, the source test and the engine's coverage
  and citation guards stop it. This stays in the follow-up (ADR 0031 Known gaps).
- **Frame locals (security N2).** Traceback frames of the getter hold the request, and so the
  key. Nothing may capture frame locals (for example an error-reporting SDK, or a logging or
  traceback option such as `capture_locals`) without a security review.
- **Gaps inside a series** are detected only by the connector's per-step check (decision 5).
  Stored or re-assessed evidence has no such check, and the engine does not detect a gap between
  instants (ADR 0008).
- **Two series, availability.** Requiring both series gives INSUFFICIENT EVIDENCE for a window
  past the end of the shorter one, even if the other series reaches it.
- **Location, availability.** The echo check and the exact match fail closed. A provider change
  to how it echoes or rounds coordinates, or to how DiveSafe records the requested point, gives
  INSUFFICIENT EVIDENCE for every item until the connector is fixed.
- **The provider's category meanings** are its own. A thunderstorm category is a forecast label,
  not a measurement, and its skill near Tioman is unknown.
- **Safety-related use is not addressed by the provider's terms (#59b).** No clause found
  either allows or forbids it, and the provider was not asked. The disclaimer puts all use at
  the user's own risk. So the use of these values in safety-related decision support rests on
  the owner's reading of the terms, not on the provider's permission. The owner accepts this
  gap at registration (condition 1). If the provider later forbids such use, the source is
  switched off. That removes NO-GO signals only: `weather` stays uncovered, so GO stays
  blocked, and nothing that was blocked becomes allowed.
- **The header form is unconfirmed for the studied endpoints.** It worked only on the free
  current-weather endpoint. Until One Call 4.0 and Storm Tracker are checked, the key may have
  to go in the query, where the provider keeps it in its request logs (decision 9), and in the
  pagination links of every paged response body, which the getter removes (see "Licence and
  price check").
- **Agents fetching the provider's website (#63b)** rests on the owner's reading that the
  published plain-text exports are a means the site purposely makes available. If the provider
  says otherwise, agents stop fetching and the owner supplies pages by hand. The fetched pages
  include instructions written for agents (the self-signup lane). Only the AGENTS.md section 12
  rule and the permissions above stand between those instructions and an agent's action.
- **The licence documents disagree** (ODbL 1.0 on the pricing pages, CC BY-SA 4.0 in the terms
  of sale). The stricter reading applies until the owner resolves it.
- **An account block stops the source until the daily reset.** Going over the account cap
  blocks every call until 00:00 to 00:05 UTC. A lapsed subscription or a failed payment answers
  401 for every call until it is fixed. Both give INSUFFICIENT EVIDENCE for every site, never
  GO. A NO-GO the source would have given becomes INSUFFICIENT EVIDENCE while it is blocked.
  Any key on the account counts toward the cap, so a leaked key can be used to block the source
  on purpose.
- **Recorded responses depend on the licence answer.** If the terms later change, recordings
  kept outside the repository may need to be deleted, and a committed recorded fixture replaced
  with a synthetic one.
- **The provider's basin boundary** may not be documented precisely enough to test against, or
  may cross the antimeridian. In either case the cyclone rule stays off (decision 26,
  condition 10).
- **One wrapping storm blinds the feed (draft 4 risk N6).** A single storm polygon that crosses
  the antimeridian anywhere in the basin rejects the whole feed. Every site then gets
  INSUFFICIENT EVIDENCE from the cyclone rule while that storm is listed. This fails closed, at
  a cost to availability.
- **A new provider zone kind blinds the feed (draft 8 risk N2).** A zone kind not in the format
  table rejects the whole feed until the table is updated. Every site then gets INSUFFICIENT
  EVIDENCE from the cyclone rule. This fails closed, at a cost to availability.
- **The held-category result is not in `/metrics`.** `api/metrics.py` (line 220, checked on
  origin/main) matches only connector prefixes, so `evidence.quality_held_category.<rule id>`
  is not counted there. It is visible only in the record and the outcome note.

## Review history

- **Draft 1** (2026-11-23): written as a study. Not reviewed.
- **Draft 2** (2026-11-23): owner answers to the 11 open questions folded in (rows 212 to 222).
  - Reviewed by `security-reviewer`: 1 blocker, 8 should-fix, 5 nits.
  - Reviewed by `risk-reviewer`: 0 blockers, 7 should-fix, 8 nits.
- **Draft 3** (2026-11-23): all findings of both draft 2 reviews folded in.
  - Owner decisions added: rows 229 (#45a) and 230 (#46a).
  - Code facts checked on origin/main and cited: `risk/engine.py`, `data/http.py`,
    `ratelimit.py`.
  - Reviewed by `risk-reviewer`: 0 blockers, 3 should-fix, 10 nits.
  - Reviewed by `security-reviewer`: 1 blocker, 4 should-fix, 6 nits.
- **Draft 4** (2026-11-23): all findings of both draft 3 reviews folded in.
  - The draft 3 security blocker (B1) is addressed: `from None` keeps `__context__`, so errors
    are raised after the `except` block ends.
  - Further facts checked on origin/main and cited: risk-model.md "off by default",
    `data/data_gov_my_forecast.py`, and line references in `data/http.py`.
  - Numbering is stable. Condition 10 (basin boundary) is appended.
  - Reviewed by `risk-reviewer`: 0 blockers, 5 should-fix, 7 nits.
  - Reviewed by `security-reviewer`: 0 blockers, 2 should-fix, 5 nits.
- **Draft 5** (2026-11-23): all findings of both draft 4 reviews folded in.
  - The held-category result id is `evidence.quality_held_category.<rule id>`. It follows the
    `risk-reviewer` draft 4 proposal; the route's scope was settled in the draft 5 review.
  - Further facts checked on origin/main and cited: the `risk/engine.py` prefixes,
    `api/schemas.py`, `api/metrics.py`, `agents/prompts.py`, `data/open_meteo_marine.py` and
    `orchestration/pipeline.py`.
  - Numbering is stable. No decision or condition is added.
  - Reviewed by `risk-reviewer`: 0 blockers, 1 should-fix, 3 nits.
  - Reviewed by `security-reviewer`: 0 blockers, 0 should-fix, 4 nits.
- **Draft 6** (2026-11-23): all findings of both draft 5 reviews folded in.
  - The citation-guard route checks a result only when it cites a held category or its factor
    reads it. The placeholder's factor-to-categories map becomes a shared, reviewed constant.
  - One Call items keep a fixed list of keys. Rule-module raises use fixed text. The cache and
    budget layer raises without a chain. Storm names go through `clip()`.
  - Further facts checked on origin/main and cited: `domain/rule_definition.py`,
    `risk/placeholders.py`, `domain/__init__.py`, `domain/observations.py`, the rule-error log
    line in `risk/engine.py`, `data/ratelimit.py` and `data/parsing.py`.
  - Numbering is stable. No decision or condition is added.
  - Reviewed by `risk-reviewer`: 0 blockers, 0 should-fix, 5 nits.
  - Reviewed by `security-reviewer`: 0 blockers, 0 should-fix, 4 nits.
- **Draft 7** (2026-11-23): all nits of both draft 6 reviews folded in.
  - The citation guard reads a result's scope from the rule object, through the shared map or
    the definition's metric. An invariant test ties the map to `FACTOR_METRICS`.
  - The `tides` wording now says the route is harmless only while `tides` is required.
  - Storm names are type-checked before `clip()`. Both item kinds keep fixed key lists, units
    are the connector's own, and the rules read values only after a type check.
  - Further facts checked on origin/main and cited: `api/state.py` line 74, `FACTOR_METRICS`,
    `_FACTOR_CATEGORIES` and `data/parsing.py`.
  - Numbering is stable. No decision or condition is added.
  - Reviewed by `risk-reviewer`: 0 blockers, 0 should-fix, 3 nits.
  - Reviewed by `security-reviewer`: 0 blockers, 0 should-fix, 2 nits.
- **Draft 8** (2026-11-23): all nits of both draft 7 reviews folded in.
  - A malformed item is a gap: the rule still reads every other item, and the ties rule
    applies.
  - A future `forecast_uncertainty` GO or CAUTION must meet the hold for every held category.
  - The storm id and zone kind are checked, a missing storm name is stored as empty text, and
    item ids are built from fixed parts.
  - Further fact checked on origin/main and cited: `bounded_text` (`data/parsing.py` lines 40
    to 48).
  - Numbering is stable. No decision or condition is added.
  - Confirmed by `risk-reviewer`: 0 blockers, 0 should-fix, 2 nits.
  - Confirmed by `security-reviewer`: 0 blockers, 0 should-fix, 1 nit.
- **Draft 9** (2026-11-23): the three draft 8 nits folded in, in the reviewers' own words.
  - Zones are grouped by storm through the item's structure, and a duplicate storm id rejects
    the feed.
  - The zone kind mapping is a cited format table, and a new kind is a Known gap.
  - The item id hash covers only fixed parts.
  - Numbering is stable. No decision or condition is added.
  - Confirmed by `risk-reviewer`: 0 blockers, 0 should-fix, 0 nits.
  - Confirmed by `security-reviewer`: 0 blockers, 0 should-fix, 1 nit (a test that item ids do
    not depend on the storm id or name), folded in as one test sentence after the round.
- **Draft 10** (2026-11-23): owner decision #57a. Adds "Licence and price check", with the
  price, the account cap, the licence, the terms and the owner's key calls, and three Known
  gaps. The status line and the pricing bullet are updated to match. Numbering is stable. No
  decision or condition is added. Risk review: S1 (condition 1 stays open, items a to d), S2
  (budget summed over workers; block and lapse test; Known gap extended), N1 to N4 applied.
  Security review: SF1 (link fields removed below the cache; page requests from a counter;
  test), SF2 (budget over all processes and restarts in the UTC day; no call after a 429 until
  00:05 UTC; leaked-key Known gap), SF4 (no share-alike conclusion; model host not assumed
  internal; licence disagreement Known gap), N1, N2, N4 to N6 applied. SF3: owner decision
  #62a (the study key is replaced before switch-on). N3: owner decision #63b (agents may fetch
  the provider's public documentation, pricing and terms pages, read-only; Known gap added).
  Confirmation rounds (2026-11-23): both reviewers, no blockers and no should-fix after round 2.
  Applied: risk N1 to N4 and round-2 N1 and N3; security SF1 (fetched pages are untrusted), N1,
  N2, N4, N5 and round-2 N1 and the first half of N2. Security N3 (a "next link present" flag)
  was withdrawn in favour of risk N2; risk's optional tighten-only link cross-check was not
  added, since no link survives the getter.
