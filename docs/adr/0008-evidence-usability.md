# ADR 0008: Evidence usability in the deterministic engine

- Status: Accepted (the `degraded_may_support_go` default needs owner confirmation)
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

## Needs the owner

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
- **A GO citing nothing** passes the citation guard; real rules must cite evidence.
- **Duplicate evidence ids** are reported as INSUFFICIENT EVIDENCE (`evidence.duplicate_ids`).

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
