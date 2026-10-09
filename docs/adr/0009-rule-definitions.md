# ADR 0009: Signed-off rule definitions

- Status: Accepted. No definition exists yet; nothing is active until a qualified person
  supplies and signs one (docs/domain-validation/).
- Date: 2026-10-09

## Context

`GO` needs a `VALIDATED` rule for every threshold factor, but the software could not represent
what a dive professional signs: a limit with its source, scope, units, forecast margin, reviewers
and an expiry. A limit scoped to something the code cannot express would have been silently
widened, and a stale rule would have kept permitting `GO` forever.

## Decision

**`RuleDefinition` (domain, pure)** is a signed-off limit as data: `factor`, `metric`
(a value a connector really provides), `worse_when` (stated, never assumed), `unit`, up to three
bands (`no_go_when`, `caution_when`, `go_when`), a `forecast_margin`, `scope` (explicit sites and an
optional maximum depth; no wildcard), `source`, `document_version`, preparer, reviews, read-back
confirmation, sign-off date and expiry. The model refuses to be built when it is inconsistent:

- a band whose comparison contradicts `worse_when`, or bands out of order (a swapped or
  mis-transcribed limit, detected only for order, direction and unit errors);
- a unit that differs from the data's unit (no conversion is ever done);
- a factor or metric with no data source (tidal current, weather, forecast uncertainty and site
  constraints cannot be encoded yet; for wind only speed can be);
- non-finite numbers, a negative margin, an empty site list.

**`VALIDATED` needs the whole sign-off:** an independent reviewer (not the preparer) for any limit
that can allow GO or CAUTION, a reviewer or recorded reason for a NO-GO-only limit, no duplicate
reviewer, a confirmed read-back, a sign-off date and an expiry after it, and an empty
`unrepresented_conditions` list. A limit scoped to something the software cannot represent
(qualification, dive type, season, exposure direction, per-factor data age) must list it there
and is therefore **refused**, not widened.

**`DefinitionRule` (risk, pure)** applies a VALIDATED definition. A plan outside its scope is not
evaluated by it. Otherwise it takes the **worst** value over the
dive window **plus the nearest instantaneous sample on each side** (a dive between two samples is
exposed to both) and adds the forecast margin in the dangerous direction; reports NO-GO, CAUTION or GO from the signed-off bands; and gives
`INSUFFICIENT EVIDENCE` for missing evidence, a different unit, a non-numeric or non-finite value,
or a value outside every signed-off range (a GO needs an explicit GO range). Results carry the
factor, the evidence ids and `VALIDATED`, so they count toward factor coverage.

**Several definitions per factor are normal** (different sites or depth tiers). One
`FactorScopeRule` per covered factor reports "no signed-off rule for this factor applies to this
site or depth" only when **none** of that factor's definitions applies; it carries no `VALIDATED`
status, so the factor stays uncovered and `GO` stays blocked. A definition being out of scope never
blocks its siblings.

**Bracketing samples are legitimate citations.** The rule cites the nearest instantaneous sample on
each side of the window, so the engine's citation guard admits exactly those (per category) as well
as in-window evidence. (A first version cited them while the guard rejected them, which made `GO`
and `CAUTION` unreachable on hourly data; an engine-level regression test now covers it.)

**Expiry is enforced by the engine,** not by the rule, and a `VALIDATED` result from a rule that
declares no expiry is refused (`rule.validated_without_expiry`), so a rule cannot forget it: a rule with `expires_at` at or before `now`
is not run and yields `rule.expired.<id>` (INSUFFICIENT EVIDENCE), so its factor is no longer
covered and `GO` is impossible until it is re-reviewed.

**Loading** (`orchestration.ruleset`, kept out of the pure `risk` package) reads the file once with
a bounded read and strict UTF-8, rejects duplicate keys and NaN/Infinity at parse time, and refuses,
one by one, anything invalid, unreviewed, expired, signed off in the future, duplicated or scoped to
a site that is not registered (so a typo cannot silently never apply). Reasons carry fixed messages
and sanitised, bounded ids; the startup error shows at most ten refusals. `DIVESAFE_RULESET_PATH` points the API at the file.
**Any refusal stops startup:** silently ignoring a signed limit is worse than not starting.
The file's **SHA-256** is computed from the exact bytes read, logged at startup, and recorded in the
ruleset version of every assessment (`label@hash12`), so an edited file with an unchanged label is
distinguishable. `DIVESAFE_RULESET_SHA256` pins the reviewed hash: startup fails if the file differs, and the pin is
checked **before** the file is parsed. In `production` the pin is required whenever a ruleset is
configured.
Placeholders remain only for factors with no definition. The repository ships no ruleset, and a
test enforces that.

## Consequences

- A professional's sign-off becomes a checked, versioned artefact; a mis-transcribed limit fails
  at load, not at a dive.
- The coverage guard still applies: one definition cannot unlock `GO` while other factors are
  unevaluated, and the degraded-data owner decision (ADR 0008) still applies.
- Five factors can be encoded today (wave height, swell, swell period, current, wind speed); four
  cannot (tidal current, weather, forecast uncertainty, site constraints). Wind gusts and direction
  are recorded in evidence but cannot be encoded: a gust-only definition would mark the whole wind
  factor as covered.

## Integrity of the file

The ruleset is **code-equivalent**: whoever can write it, or change `DIVESAFE_RULESET_PATH`, can
change what the system recommends, and the sign-off fields are text that nothing authenticates. So:
the file must be read-only for the application user, owned by a deploy user, changed only through a
reviewed change, and pinned with `DIVESAFE_RULESET_SHA256`. The path and the pin must not be
settable by untrusted parties. A detached signature would be stronger and is not built.

## Not solved

- **Scope the software cannot express:** qualification, dive type, season, exposure direction and
  per-factor data age. They are recorded and cause refusal.
- **A consistently wrong but plausible value loads.** The model catches order, direction and unit
  errors, not a wrong number or the wrong quantity; the read-back is the only control for those.
- **`unrepresented_conditions` is declared by the author.** A condition they omit is silently
  widened; the sign-off checklist is the only control.
- **Independence is a casefolded name comparison** (`J. Smith` and `John Smith` differ), and the
  read-back must be confirmed by the preparer or a reviewer, not by the developer, by name only.
- **Ties:** bands are evaluated NO-GO, then CAUTION, then GO, so equal values resolve to the more
  severe outcome, and a gap between bands gives no recommendation (tested).
- **Bracketing samples are taken at any distance, with no quality or age filter.** A distant or stale
  sample can force NO-GO (conservative, but it costs availability), and a nearer low-value sample
  that the engine judges unusable can displace an older valid high one, in which case the result
  cites an unusable item and is downgraded to INSUFFICIENT EVIDENCE, hiding a NO-GO. Neither can
  produce GO.
- **Symlinks are followed** when reading the ruleset path; the file is trusted configuration and
  the pin covers its content.
- **No maximum validity period:** the expiry is whatever was signed; choosing a cap is a policy
  decision with a number that must come from the owner.
- **One primary metric per factor:** secondary quantities (for example wind-wave height) cannot
  be encoded, because they would otherwise mark the whole factor as covered.
- **A NO-GO-only limit** with a recorded reason can be prepared and read-back-confirmed by one
  person; only limits that can allow GO or CAUTION require an independent reviewer.
- **A reviewer's identity is a string.** Nothing proves the signature is genuine; the signed
  document is the evidence and a human must hold it. A definition file needs access control and
  change review like code.
- **A limit that is the same unit but the wrong quantity** (for example maximum instead of
  significant height) is a transcription risk the read-back step must catch; the model checks the
  metric name, not the meaning.
- **Margin is a single number** in the data's unit, applied to the worst value; it does not model
  forecast skill that varies with lead time.
- **Direction conventions** come from the provider's documentation and are unverified.
