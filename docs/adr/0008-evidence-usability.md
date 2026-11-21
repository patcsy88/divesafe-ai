# ADR 0008: Evidence usability in the deterministic engine

- Status: Accepted (owner confirmed `degraded_may_support_go=False` on 2026-10-10)
- Amended 2026-11-20 (Amendment 1, owner): degraded evidence may support GO and CAUTION when
  `DIVESAFE_DEGRADED_MAY_SUPPORT_GO=true`; see Amendment 1 at the end.
- Amended again 2026-11-20 (Amendment 2, owner): with the setting on, tide predictions satisfy the
  required `tides` category for GO and CAUTION; tides no longer need observations; no other DEGRADED
  tide item can satisfy the category. See Amendment 2 at the end.
- Date: 2026-10-09

## Context

The engine only checked that evidence of a required category had been *retrieved* recently. It
ignored the time the data refers to, so a fresh fetch of data for the wrong hour counted, and it
ignored `quality`, which a connector sets itself. Open-Meteo model data (coarse, "not suitable for
navigation") therefore fully satisfied the currents requirement.

## Decision

**Usable evidence** for a required category is evidence that is all of:

1. **Fresh:** `retrieved_at` within `max_age` and not in the future.
2. **Of accepted effective quality:** `EvidencePolicy.accepted_quality` (default VALIDATED and
   DEGRADED; UNASSESSED and REJECTED refused), judged on `effective_quality`, never on the claim.
3. **Covering the dive window,** unless exempt. Intervals (`valid_until` set) must chain with no
   gap from the planned start to the planned end; a gap is detected without any invented number.
   Instants can only be bracketed (see "Not solved"). `require_window_coverage=False` is an
   explicit opt-out.

**The exemption is an allow-list of (kind, category)**, not a kind: NOTICE only for
`marine_warnings`; KNOWLEDGE only for `site_information`, `local_guidance` and
`historical_observations`. A warning or guidance matters whether or not it spans the window, but
only while it has not already ended before the dive. Live-condition data labelled as a notice or
knowledge therefore gets no exemption and must cover the window.

Failure gives `INSUFFICIENT EVIDENCE` with the specific reason (missing, stale, not of accepted
quality, or not covering the window), which the API shows verbatim.

**Effective quality** (`divesafe.domain.effective_quality`, pure) caps the connector's claim.
VALIDATED is honoured only for a measured OBSERVATION that records both a `unit check` and a
`range check` (shared constants `UNIT_CHECK`, `RANGE_CHECK`) and has no quality notes. Models,
forecasts, predictions, notices and knowledge are capped at DEGRADED, because checking that a
model's output is plausible does not validate it.

**An observation cannot describe time after it was retrieved.** `EvidenceItem` rejects an
OBSERVATION whose validity extends past `retrieved_at`. Consequence: only a forecast or model can
cover a future dive, so nothing can earn VALIDATED for a future window, and `GO` for a future dive
needs the owner decision below.

**Degraded data and GO/CAUTION.** DEGRADED evidence can make a category *sufficient*, but
`degraded_may_support_go` defaults to `False`. If the result would be GO or CAUTION, every
required category needs VALIDATED evidence that **covers the window on its own**; a validated
item at another time cannot launder a degraded item that covers the dive. Otherwise the engine
appends `evidence.quality_insufficient_for_go` (INSUFFICIENT EVIDENCE).

**A GO or CAUTION may not cite unusable evidence.** If a rule's GO/CAUTION cites an evidence id
that is stale, of unaccepted quality or unrelated to the dive window (does not overlap it and is
not exempt), or that is not in the evidence at all, the engine appends
`rule.cites_unusable_evidence.<rule_id>` (INSUFFICIENT EVIDENCE).

**Rules still receive all evidence.** This is deliberate: hazard-detecting rules (for example the
warning rule) must see stale or unaccepted items, because filtering could hide a real hazard. The
citation guard is what keeps a GO from resting on them.

## Owner decision (2026-10-10)

The owner chose to keep `degraded_may_support_go=False`. A degraded forecast or model never
supports GO or CAUTION. While no source can be VALIDATED for a future window, GO and CAUTION stay
unreachable for future dives, and the system returns NO-GO or INSUFFICIENT EVIDENCE. Reconsider
only with written limits from a dive professional and a risk-reviewer sign-off, recorded here.

## Was open: needs the owner

Whether a regional model forecast (DEGRADED) may ever support GO or CAUTION is a risk decision.
The default is "no". Changing it is a one-line policy edit that must be reviewed by `risk-reviewer`
and recorded in this ADR, together with the compensating controls.

## Consequences

- A connector can no longer unlock GO by labelling its output VALIDATED.
- Out-of-window data cannot satisfy a requirement however fresh the fetch.
- Unreviewed `accepted_quality`, the cap rule and the exemption list are policy: changes need review.

## Not solved (see docs/risk-model.md)

- **Per category, not per variable or location:** a covering item with a different variable or
  grid cell satisfies the category. A reviewed required-variable and location rule is needed.
- **A GO or CAUTION citing nothing** is now refused (fixed 2026-10-26: flagged `CITES_UNUSABLE`, so the result is INSUFFICIENT EVIDENCE); real rules must cite evidence. A citation of a knowledge item is not yet flagged (ADR 0022 decision 8).
- **Duplicate evidence ids** are reported as INSUFFICIENT EVIDENCE (`evidence.duplicate_ids`);
  since 2026-10-25 that rule can fire only for direct engine callers.
  Update (2026-10-25, owner decision): `gather_evidence` and `build_evidence_set` no longer raise on
  two different items under one id; every item with that id is dropped and a fixed
  `evidence.conflict` issue is recorded, then `evidence.connector_issues`, so the result is
  INSUFFICIENT EVIDENCE. **A conflict can hide a more severe result** (an item that would have
  crossed a signed-off limit, or a marine warning): the result is INSUFFICIENT EVIDENCE instead of
  NO-GO. This was latent while no validated ruleset was loaded. Since 2026-11-21 production loads
  a signed ruleset (ADR 0024 A8), so it is live for conflicts beyond the ADR 0026 capacity bounds
  (see the variant search below; accepted by the owner, 2026-11-21). A human can override
  INSUFFICIENT EVIDENCE, so the dropped items are kept in the audit record as withheld evidence
  (not cited, not evaluated, not passed to the engine, agents or prompts) and shown clearly
  labelled to the human; the conflict issue names the category and says the withheld evidence was
  not in the main result (ADR 0026 reads them only to add severity). A source whose only items were dropped is left out of `data_versions`; the
  withheld items keep their own `source_version`. **Built later (ADR 0026, 2026-11-05):** the
  most severe variant decides; the engine runs on each combination and adds an
  `evidence.conflict_variant` rule result, so a hidden NO-GO stays NO-GO (this closes the gap
  above). **Known gaps:** the record validator checks shape only (an
  `evidence.conflict:` issue exists and each withheld id is on at least two different items), not
  that the items match the issue's categories or connectors, and sets no count limit on withheld
  items. "Withheld" has three meanings; see the terms block in
  [architecture.md](../architecture.md).

- **Gaps inside a series of instants:** an instant has no extent, so detecting a hole needs a
  maximum gap, a number that must come from a reviewed source. The marine connector guarantees
  hourly continuity per category; the engine does not check it.
- **Self-asserted checks:** a connector could add transformation steps named `unit check` and
  `range check` without performing them. No production connector claims VALIDATED, and nothing can
  earn it for a future window, so this is dormant; before any connector is allowed to claim
  VALIDATED, require a reviewed allow-list of (source, version) pairs or have domain code stamp
  the checks.
- **`marine_warnings` can never be VALIDATED** (a notice is capped at DEGRADED), so under the
  default policy it permanently prevents GO. That is intended.
- **A policy with no required categories** skips both sufficiency and quality checks; the
  pipeline refuses it and production configures categories.
- **Contradictory evidence** is still not detected.
- **Hand-set policy knobs** (`degraded_may_support_go=True`, `required_factors=frozenset()`,
  `require_window_coverage=False`) are public constructor arguments. A safety test scans `src/`
  to ensure none is used there, and another asserts the production engine's defaults.
  Production now passes `degraded_may_support_go` from `Settings` (ADR 0008 Amendment 1), a
  computed value that the scan does not catch by design. A separate safety test pins that
  `build_engine` is the only place it is set, and the Settings check, the `build_engine` re-check
  and the definitions check guard it. The marker reads required categories only, so a GO whose
  rule also read a DEGRADED item from a category that isn't required is not marked; that can only
  tighten.

## Amendment 1: degraded evidence may support GO and CAUTION (2026-11-20, owner)

The owner reverses the 2026-10-10 decision for production. `degraded_may_support_go` becomes a
setting, `DIVESAFE_DEGRADED_MAY_SUPPORT_GO` (default `false`), which the owner sets to `true` in
production. With it on, DEGRADED evidence (models, forecasts, predictions, notices, knowledge)
that is fresh and covers the dive window may support GO or CAUTION, and
`evidence.quality_insufficient_for_go` is no longer added. UNASSESSED and REJECTED data stay
refused.

Unchanged: freshness, window coverage, the citation guard, required categories, placeholder
factors (INSUFFICIENT EVIDENCE), the marine-warning rule, connector issues, every NO-GO limit and
the human gate.

Controls: (1) start-up refuses `true` unless a signed-off ruleset is loaded with its SHA-256
(`DIVESAFE_RULESET_PATH`, `DIVESAFE_RULESET_SHA256`); (2) a GO or CAUTION that rests on DEGRADED
evidence carries a fixed note, "based on model and forecast data, not observations", shown to the
human on the assessment's web page and in the decision-form context; (3) start-up logs a warning
while it is on. Before GO or CAUTION can first become reachable (for example with the tides
follow-up), check that the deployed web build includes `DegradedEvidenceNote`: open a synthetic
marked record on a staging deploy of the same web build, or confirm the note text is in the
deployed bundle. The web UI is deployed separately from the API, and nothing in the code checks
this.

Per category (owner, 2026-11-20): DEGRADED `tides` evidence never supports GO or CAUTION, whatever
the setting says. `risk.DEGRADED_NEVER_SUPPORTS_GO` (a reviewed constant, not a setting) names the
categories. With the setting on, the engine still adds `evidence.quality_insufficient_for_go` when a
named category has no VALIDATED evidence that covers the dive window on its own, and names only
those categories. Reason: a tide prediction is for a reference point, not the dive site, and a
height or time gives no current (ADR 0021, ADR 0029 decision 6). Tested in
`tests/safety/test_degraded_support_policy.py` (section i), mutation-checked. The hold works
through the required-category check, so it acts only while `tides` is a required category; a
safety test pins `tides` in the production categories. While tides are required and no measured
tide source exists, no site can reach GO or CAUTION, even with the setting on. (Superseded by
Amendment 2 for tide predictions.)

Basis: written limits in the owner's signed-off ruleset (from a local dive professional's current
bands and the owner's mapping of them). The basis covers only factors with signed-off rules; the
others stay at INSUFFICIENT EVIDENCE.

Per category and granularity (owner, 2026-11-20): with the setting on,
`evidence.quality_insufficient_for_go` is still added for a required category whose DEGRADED cover
of the dive window comes only from items valid for a day or longer (`risk.COARSE_SPAN`, such as
MET's daily ranges): a daily item does not stand in for time-resolved evidence. Tested in section
j, mutation-checked. Still not built: a tighten-only role for MET's daily ranges (an official
forecast that disagrees does not tighten a result); this is owner-accepted while four threshold
factors cannot be encoded and GO and CAUTION are unreachable in production. What holds this:
`tests/safety/test_tide_is_not_a_current.py` keeps `tidal_current` without a metric, so
`threshold_factors_uncovered` blocks GO; giving it a metric turns that test red and reopens this
acceptance. `COARSE_SPAN` passes the numeric-literal scan only because small integers are allowed;
`test_the_coarse_span_is_one_day` is what protects its value.

**Owner condition (2026-11-21), superseding the acceptance above for switching the setting on:**
MET's tighten-only role must be built and reviewed before `DIVESAFE_DEGRADED_MAY_SUPPORT_GO` is
switched on, or before any other change that could let GO or CAUTION be the result for any site. The
first signed ruleset (deployed 2026-11-21, ADR 0024 A8 "Deploy of `d981a6a`") has wind and
wave-height definitions, and none reads MET's daily ranges. The gap stays accepted only while GO and
CAUTION are unreachable. Enforced in code for the setting (2026-11-21; in production from the
deploy that includes it): start-up refuses `true`, even with a pinned ruleset, while
`MET_TIGHTEN_ONLY_ROLE_BUILT` in `config/settings.py` is `False` (`Settings` and again in
`build_engine`; `tests/safety/test_degraded_needs_met_role.py`). Only the PR that builds and
reviews the role sets it to `True`, and it must change that safety test with it, so risk-reviewer
reviews that PR (AGENTS.md section 13). Other changes are pinned by a safety test
as far as it can see: `tidal_current`, `weather`, `forecast_uncertainty` and `site_constraint`
have no encodable metric, and a ruleset with a GO band for every encodable factor still gives
INSUFFICIENT EVIDENCE on VALIDATED evidence. A change it cannot see (for example a new kind of
rule) rests on review (architecture.md Known gaps).

**Update 2026-11-21 (ADR 0031):** MET's tighten-only role is built (`risk/met_ranges.py`) and
`MET_TIGHTEN_ONLY_ROLE_BUILT` is `True`. Start-up refuses `true` without the MET token, and accepts
it with a pinned ruleset that has definitions and with the token. The owner's separate decision to
switch it on is not enforced in code. The setting stays `false` in production until that decision.
The condition's other half is unchanged and still pinned: GO and CAUTION stay unreachable while
four factors cannot be encoded.

Consequence: switching the setting on alone does not make GO reachable. Factors without a deployed
rule, a missing required category and any marine warning still give INSUFFICIENT EVIDENCE.

risk-reviewer sign-off (2026-11-20): no blockers. Reviewed the setting, the Settings and
`build_engine` refusals, the per-category `tides` hold, the coarse-daily hold, the
`rests_on_degraded_evidence` marker and the fixed note in the API and web UI. The setting is the
owner's decision under ADR 0008 Amendment 1. The hold and the marker never lower a severity: the
hold only adds INSUFFICIENT EVIDENCE, and the marker is not a severity. DEGRADED tides cannot
support GO or CAUTION while `tides` is a required category, which a test pins. Placeholders,
factors that can't be encoded, the marine-warning rule and the human gate are unchanged, so with
the current code GO and CAUTION remain unreachable in production. The coarse-daily item is
enforced by the built rule (`risk.COARSE_SPAN` and `RiskRulesEngine._fine_cover` in
`_quality_guard`, tested in section j of `tests/safety/test_degraded_support_policy.py`,
mutation-checked as the author reports). The MET daily-range tighten-only role is not built; the
owner accepts this gap while `tidal_current` and three other threshold factors cannot be encoded
(pinned by `tests/safety/test_tide_is_not_a_current.py`), which keeps GO and CAUTION unreachable in
production. The sign-off covers the code and the stated controls in this change only, not the
planned relaxation of the tides hold for predictions, which needs its own review. It does not
cover the owner's ruleset limits or the owner's judgement on the model current, which are recorded
as the owner's.
(Re-checked 2026-11-20 after the `EvidencePolicy` `KW_ONLY` change.) (The tides sentence is
superseded by Amendment 2, 2026-11-20.)

## Amendment 2: tide predictions may support GO and CAUTION (2026-11-20, owner)

The owner decides that tides no longer need to be observed: a tide prediction is adequate. The owner
narrows Amendment 1's per-category hold on tides to everything except predictions.
`risk.DEGRADED_NEVER_SUPPORTS_GO` still names `tides`; the one way out is DEGRADED tide PREDICTIONs
that cover the dive window on their own. No other DEGRADED tide item (model, forecast or a degraded
gauge reading, however short its validity) can satisfy the category. A PREDICTION in
`risk.TIME_RESOLVED_PREDICTIONS` (`tides` only) is also time-resolved, not coarse: a tide day lists
every high and low water with its time, so the `COARSE_SPAN` rule does not hold it. The exemption
checks the item's kind and category, not its content: the tides.today parser can accept a day with
one high/low pair missing. A new tide PREDICTION source needs its own review before it relies on
`TIME_RESOLVED_PREDICTIONS`. With `DIVESAFE_DEGRADED_MAY_SUPPORT_GO` on, a DEGRADED tide prediction
that is fresh and covers the dive window therefore satisfies the required `tides` category for GO or
CAUTION; no rule reads it. The result is marked for the human's fixed note. With the setting off,
nothing changes. A daily prediction in any other category stays coarse.

Unchanged: a tide prediction is for a reference point, not the dive site (tides.today's Teluk Tekek
point is about 13.4 km from Tiger Reef), is astronomical only, and gives no current: `tidal_current`
stays unencodable (`tests/safety/test_tide_is_not_a_current.py`), no rule reads tides, and the
tides.today permission's conditions (never the sole source; a human decides) still apply. This
change alone does not make GO reachable in production. The factors that cannot be encoded are now
the only code barrier to GO, so any change that encodes or drops one of them needs a full risk
review.

Tested in section i of `tests/safety/test_degraded_support_policy.py` (replacing Amendment 1's
tide-hold tests, with the owner's approval) and in the tides.today and PHN evidence tests;
mutation-checked. Supersedes ADR 0021 decision 4 and ADR 0029 decision 6 where they say the setting
must not be turned on for tides. Risk-reviewer sign-off: see below.

risk-reviewer sign-off (2026-11-20): no blockers. Reviewed the narrowing of Amendment 1's tides
hold, `risk.TIME_RESOLVED_PREDICTIONS`, `RiskRulesEngine._predictions_cover` and the exemption in
`_fine_cover`, the guard rationale, and the replaced tests in section i of
`tests/safety/test_degraded_support_policy.py` and in the PHN and tides.today evidence tests
(mutation-checked as the author reports; the one surviving mutant, the kind check in `_fine_cover`,
is equivalent while `TIME_RESOLVED_PREDICTIONS` is a subset of `DEGRADED_NEVER_SUPPORTS_GO`). This
relaxation is the owner's decision. With `DIVESAFE_DEGRADED_MAY_SUPPORT_GO` on, DEGRADED tide
PREDICTIONs that are fresh and cover the dive window on their own satisfy the required `tides`
category for GO or CAUTION, and the result is marked for the human's fixed note; no other DEGRADED
tide item (model, forecast or reading, however short) can satisfy the category. With the setting
off, nothing changes. A daily prediction in any other category, and MET and other daily items, stay
coarse. No rule reads tide values: a tide prediction is for a reference point, not the dive site,
and gives no current. `tidal_current` and three other threshold factors still cannot be encoded
(pinned by `tests/safety/test_tide_is_not_a_current.py`), so with the current code GO and CAUTION
remain unreachable in production. That is now the only code barrier, so any change that encodes or
drops one of those factors needs its own risk review. The sign-off covers this change only. It does
not cover the owner's judgement that tide predictions are adequate, which is recorded as the
owner's.

*Note (2026-11-21): PHN e-Tide was removed (ADR 0021 WITHDRAWN); the PHN evidence tests
(`tests/safety/test_phn_tide_evidence.py`) no longer exist. Each of their cases has a tides.today
twin in `tests/safety/test_tides_today_evidence.py`.*
