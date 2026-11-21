# ADR 0031: MET's daily ranges may only tighten

Status: Accepted (owner 2026-11-21; reviewed by risk-reviewer and security-reviewer,
2026-11-21).

## Context

`MetApiMarineConnector` gives a daily range for each +08:00 day for MET's waters zone
`LOCATION:499` (TIOMAN): wind speed (`FMWS`, km/h) and wave height (`FMWH`, m). Each item is
FORECAST and DEGRADED, and the ranges are under `value["ranges"]`, which no rule reads. So a MET
range in a NO-GO band gives no NO-GO (ADR 0024 A8, the "MET `ranges` unread" gap). The owner
condition of 2026-11-21 (ADR 0008 Amendment 1) requires a tighten-only role for these ranges
before `DIVESAFE_DEGRADED_MAY_SUPPORT_GO` is switched on. Until then, start-up refuses the flag
(`MET_TIGHTEN_ONLY_ROLE_BUILT`).

## Decision (owner, 2026-11-21)

1. **One MET check per signed-off wind or wave-height definition.** `build_engine` adds a
   `MetRangeTightenRule` for each loaded definition whose metric is `wind_speed_10m` (paired with
   MET `wind_speed`) or `wave_height` (paired with MET `wave_height`). No other metric is paired.
2. **The definition's own limits (owner answer 1, A).** The check uses only that definition's
   `no_go_when` and `caution_when`, and adds no number of its own. MET does not state the height
   of its wind or which wave height it gives. That is accepted because the check can only tighten.
3. **Margin added (owner answer 2).** The value tested is the highest `high` across the +08:00
   days the dive window touches, plus the definition's `forecast_margin` (for a lower-is-worse
   definition, the lowest `low` minus the margin).
4. **Results.**
   - In the NO-GO band, the result is **NO-GO**. In the CAUTION band, the result is **CAUTION**.
   - Otherwise there is **no result**.
   - The check never gives GO, carries no VALIDATED `threshold_status`, and never counts as
     covering a factor. It can only add severity to the paired definition's result, which is
     always present when the check applies.
   - It cites the MET item ids and names the definition, and shows the value, the margin and the
     band.
5. **Approved zone links only (owner answer 3, A; answer 4, both).** The check applies only when
   the definition applies to the plan (site and depth) and the site is in a fixed, reviewed list of
   approved links in code. A site outside the list gets no MET check. Adding a site is the owner's
   decision, one site per decision, through review.
   - `my-pahang-pulau-tioman` → `LOCATION:499` TIOMAN (approved 2026-11-21).
   - `my-pahang-tiger-reef` → `LOCATION:499` TIOMAN (link decided 2026-11-20, ADR 0029
     Amendment 2; approved for this check 2026-11-21).
6. **Missing or unusable MET data (owner answer 5, A).** For an approved site, any of these gives
   **INSUFFICIENT EVIDENCE**, with a fixed rationale:
   - no MET item for a day the window touches;
   - a missing or wrong unit;
   - a non-numeric or non-finite value;
   - a malformed range;
   - an item for another zone.

   Start-up refuses `DIVESAFE_DEGRADED_MAY_SUPPORT_GO=true` without the MET token (secret file
   `divesafe_met_api_token` in production, or `DIVESAFE_MET_API_TOKEN` outside it). Without the
   token there is no MET connector, so the check would always give INSUFFICIENT EVIDENCE.
7. **Always on.** The check runs whether the DEGRADED flag is on or off, because it only tightens.
   With today's ruleset it can turn INSUFFICIENT EVIDENCE into NO-GO, never into GO or CAUTION.
   A DEGRADED MET item may raise GO to CAUTION with the flag off. This tightens, so it is not
   "supporting" CAUTION in AGENTS §7's sense, and the quality guard still applies to the result.
8. **The gate.** The same PR sets `MET_TIGHTEN_ONLY_ROLE_BUILT = True` and changes
   `tests/safety/test_degraded_needs_met_role.py`. That only allows the flag. Production keeps
   `false` until the owner changes it, and the switch-on is a separate owner decision. The other
   half of the owner condition stands: GO and CAUTION stay unreachable while four factors cannot
   be encoded, and the test stays. Start-up accepts `true` with a pinned ruleset that has
   definitions and with the MET token. The owner's separate decision to switch it on is not
   enforced in code.

## Consequences

- **Closes the "MET `ranges` unread" gap** for wind and wave height, for an approved site where a
  paired definition applies (site and depth). With the deployed ruleset `tiger-reef-2026-11-20`
  that is Tiger Reef within the definitions' depth only; for Tioman, which has no definition, the
  gap stays open until a Tioman definition is signed.
- **The tighter of the two decides.** When MET's top value is above the hourly data, the worse
  result wins. When it is below, the hourly result stands.
- **Live effect:** a dive whose MET day range, plus margin, crosses a NO-GO limit now gives NO-GO
  instead of INSUFFICIENT EVIDENCE. A human can still override it, with the `overrider` role and a
  rationale.
- **Not covered:**
  - wind gusts and direction (MET gives a direction code only);
  - swell;
  - a MET value from before the day's issue (no issue time is given, so age is retrieval age
    only);
  - the zone's extent (MET does not state it).

## Tests (safety, mutation-checked)

- NO-GO and CAUTION bands, including band edges with the margin.
- No result below the CAUTION band; never GO.
- The check never covers a factor.
- A site outside the approved list gets no check.
- Missing day, wrong unit, non-numeric, non-finite, other zone: INSUFFICIENT EVIDENCE.
- A multi-day window uses the worst day.
- The DEGRADED flag is refused without the MET token.
- `build_engine` adds exactly one check per paired definition, and each `met-range.X` has a
  matching `definition.X`.
- A stale rough item gives NO-GO; a stale calm item gives no result.
- A negative `low` and an int too large for a float (`10**400`) give INSUFFICIENT EVIDENCE.
- The wind rationale carries the not-evaluated marker for gusts and direction.
- A test pins that the check cannot lower a result where its paired definition applies (with
  empty and with default `required_factors`).

## Implementation (2026-11-21)

- `risk/met_ranges.py`: `MetRangeTightenRule`, `MET_TIGHTEN_ZONES` (the approved links, read-only)
  and `MET_PAIRED_METRICS`. `risk` cannot import `data`, so the links are a copy of the
  connector's `WATERS_ZONES`; a safety test pins that the two agree.
- Rule id `met-range.<definition id>`, not a reserved prefix. The check expires with its
  definition (`expires_at`), so an expired limit is never applied, not even to tighten.
- The result has no `factor` and no `threshold_status`. That is how it never covers a factor, and
  why the `RuleResult` validator allows its CAUTION without a VALIDATED status.
- Only items from `met-api-marine-forecast` of the definition's category count. Days are
  half-open: a dive ending at midnight does not need the next day. Coverage is read from the
  validity windows, never from the `date` text.
- Every MET item of that category in the window must be usable; a bad one is never skipped, even
  next to a good one for the same day. An instant (no `valid_until`), a negative `low` or a `low`
  above `high` also gives INSUFFICIENT EVIDENCE.
- A wind result says that gusts and direction were not evaluated, as the definition's does.
- A lower-is-worse definition uses the `low` end minus the margin. Neither paired metric is
  lower-is-worse today.
- `Settings` and `build_engine` refuse the DEGRADED flag without the MET token
  (`DEGRADED_NEEDS_MET_TOKEN`; fixed text, the token is never echoed). `MET_TIGHTEN_ONLY_ROLE_BUILT`
  is `True`. The refusal while it is `False` stays tested, with the gate pinned closed.
- Tests: `tests/safety/test_met_tighten.py`, plus changes to `test_degraded_needs_met_role.py` and
  `test_degraded_support_policy.py`. 32 mutants were run and all were killed.

## Known gaps

- **Age is not checked by the check.** A rule does not get the clock. A stale MET item in a NO-GO
  band still gives NO-GO (it can only tighten). A stale calm item gives no result, so it does not
  give INSUFFICIENT EVIDENCE. The hourly data and the engine's freshness check for required
  categories still apply. Both behaviours are pinned by a test.
- **A result with no factor and no status passes the `RuleResult` validator for GO or CAUTION.**
  This check never returns GO, but the validator does not stop another rule shaped like it; only
  the engine's coverage and citation guards do. Not new with this ADR; a follow-up.
- **The check runs only when a paired definition applies.** A site in the list whose definition
  scope or depth does not cover the plan gets no MET check (`FactorScopeRule` reports the
  missing definition).
- **The approved list is a copy.** Adding a site to the connector's table does not add the check,
  and the other way round; the pinning test makes the two be changed together.
- **Without the token and with the flag off**, an approved site with a paired definition gets
  INSUFFICIENT EVIDENCE from this check (no MET items). This only tightens, and production has the
  token.
