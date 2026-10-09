# Questionnaire

Answer what you can, with sources. "Not assessed" is an honest answer, but while any factor is
unassessed the software cannot say `GO` or `CAUTION` (see the README). Please read the
[README](README.md) first, and use the [rule template](rule-template.md) for every limit.

Nothing in this document is a recommended limit. **No numbers are suggested on purpose.**

## Part A: decisions about caution

These need a person with authority over safety policy. Each has a safe default that the software
uses until you answer.

### A1. May a regional model forecast ever support a GO or CAUTION?

The only data we hold for waves, swell, currents and temperature are coarse regional model
forecasts, which the system grades "degraded". Even with perfect rules, the software currently
refuses `GO` **and** `CAUTION` on degraded data alone. This is one switch for the whole system.

- [ ] Never. A recommendation needs measured or validated site data. (**software default today**)
- [ ] Yes, but only for factors you name, with a stated safety margin: ________________
- [ ] Yes, with a mandatory briefing step recorded by the dive leader: ________________

The second and third options are **requests for new features**; the software has no per-factor
switch, margin or briefing step today. If you choose one, the developer will build and review it
before it has any effect.

Your reasoning (and what protects the diver if the model is wrong): ________________

### A2. Marine warnings

Today **any** MET Malaysia warning that overlaps the dive window, and **any** undated notice
(whatever its date), stops an automatic GO, because the notices are free text covering several sea
areas and the software will not guess whether one applies. This includes routine notices such as
a "No Advisory" tropical-cyclone message.

- Which notice titles are irrelevant to a recreational dive? ________________
- Which always mean no-go? ________________
- Which named sea areas cover each site? (Please give your source.) ________________

### A3. When sources disagree

If two sources give different values for the same factor, the software currently does not detect
it. Should the advice always become more cautious? by how much? (Agreement between two sources
only counts as confirmation if they are independent: some sources we use are built on the same
upstream product, and the software records this. Please say how large a difference between
independent sources should matter, per factor.) ________________

### A4. Data age and look-ahead

Please give a maximum age and a maximum look-ahead per factor, with a reason. (The software has
no default; today the age limit is set by the operator.) ________________

### A5. Who may override

Today any authenticated user can override the system's advice, including a NO-GO, with a written
reason. Should a NO-GO override need a second person, a particular qualification, or be
disallowed? ________________ (The override record is only trustworthy once the API's
authentication is deployed.)

## Part B: one form per risk factor

For every factor: what data we hold (so you can judge whether it can support a limit), then
questions. Add one rule form per limit you supply.


### B1. Wind (`wind`)

**Status: REQUIRES DOMAIN VALIDATION.** The software has no limit for this factor and reports it as *not evaluated*.

**Data we hold:** Hourly regional model forecasts for wind at 10 m above the surface: wind speed (kilometres per hour, an instantaneous value), wind gusts (kilometres per hour, the maximum of the **preceding** hour) and wind direction (degrees; the convention, whether this is the direction the wind blows from, is not stated in the provider documentation we read and is to be verified before you rely on it). The provider chooses the underlying weather model automatically and does not tell us which one produced a value; resolution depends on that model. The values are model output for a grid cell the provider picks (we ask for a sea cell, so it may lie offshore of a shore entry or a lee shore, and the distance from the dive point is recorded), never a measurement at the dive site. A dive between two hourly values is judged on the worst of the surrounding values, and the wind result will always say that gusts and direction were not evaluated. Only wind speed can be encoded as a limit today. Wind can also appear in the free text of a MET Malaysia warning.

**Questions:**

1. Would you use wind speed, gusts, direction, or all three? (Only speed can be encoded today.)
2. Does it matter more for boat transfers, surface conditions or shore entry?
3. What is the wind limit for each diver level and dive type?
4. Does direction relative to the site's exposure change the limit?

**Your answer / rule forms attached:** ________________

- [ ] Not assessed (the factor stays TBD)


### B2. Wave height (`wave_height`)

**Status: REQUIRES DOMAIN VALIDATION.** The software has no limit for this factor and reports it as *not evaluated*.

**Data we hold:** Hourly regional model output: significant wave height (metres), plus wind-wave height, wave period (seconds) and wave direction (degrees; per the provider's documentation the direction the waves come FROM, to be verified before you rely on it). Grid cells are about 8 to 25 km across and the provider snaps to one cell (about 5 km from the Tioman reference point in the recordings we hold). The provider says coastal accuracy is limited and the data is not suitable for navigation. It is a forecast or model value, never a measurement at the dive site.

**Questions:**

1. Which wave quantity do you use? Available as data: significant height, wind-wave height and swell height. Only significant height can be encoded as a limit today. A maximum or individual wave height is not available.
2. What limit, per diver level and dive type, for entry/exit and for the surface?
3. How much should the system add for the model being coarse and not site-specific?
4. Is there a sheltered or exposed side of the site that changes the limit?

**Your answer / rule forms attached:** ________________

- [ ] Not assessed (the factor stays TBD)


### B3. Swell (`swell`)

**Status: REQUIRES DOMAIN VALIDATION.** The software has no limit for this factor and reports it as *not evaluated*.

**Data we hold:** Hourly regional model output: swell wave height (metres) and direction (degrees, direction FROM per the provider's documentation, to be verified). Same resolution and accuracy limits as wave height. The model gives the main swell; secondary components are not requested.

**Questions:**

1. Is swell judged separately from total wave height?
2. Does swell direction relative to the site's exposure change the limit? Please describe the exposure of each site you cover.
3. What swell limit, per diver level and dive type?

**Your answer / rule forms attached:** ________________

- [ ] Not assessed (the factor stays TBD)


### B4. Swell period (`swell_period`)

**Status: REQUIRES DOMAIN VALIDATION.** The software has no limit for this factor and reports it as *not evaluated*.

**Data we hold:** Hourly regional model output: swell period (seconds). Same resolution and accuracy limits as above.

**Questions:**

1. Does period matter independently of height (for example for surge or entry/exit)?
2. What limit, if any, and for which diver level, dive type and site?
3. Is there an interaction between period and height you want treated as one rule?

**Your answer / rule forms attached:** ________________

- [ ] Not assessed (the factor stays TBD)


### B5. Current (`current`)

**Status: REQUIRES DOMAIN VALIDATION.** The software has no limit for this factor and reports it as *not evaluated*.

**Data we hold:** Hourly regional model output: ocean current speed (kilometres per hour) and direction (degrees, the direction the current is heading TO per the provider's documentation, to be verified). The provider states it is computed at about 8 km resolution, includes tide and wave effects, and has limited accuracy near the coast. It cannot resolve reef-scale or channel currents at a dive site.

**Questions:**

1. What current limit, per diver level and dive type (drift, moored, shore)?
2. Does direction relative to the site or the boat matter?
3. Is this data adequate to support a limit, and why or why not? If it is not, say so: the factor can stay unassessed until a better source exists.

**Your answer / rule forms attached:** ________________

- [ ] Not assessed (the factor stays TBD)


### B6. Tidal current (`tidal_current`)

**Status: REQUIRES DOMAIN VALIDATION.** The software has no limit for this factor and reports it as *not evaluated*.

**Data we hold:** None. There is no verified tide or tidal-stream source. Official Malaysian tide predictions exist (printed tables, and a mobile app with a 7-day forecast) but we have found no licensed, machine-readable source. The model's sea-level value is relative to global mean sea level and is NOT a tide prediction, so the system does not use it.

**Questions:**

1. Do you plan around slack water or tidal windows at your sites? Which sites?
2. What tidal-stream limit applies, per diver level and dive type?
3. Which official or local source would you trust for predictions, and can we obtain a licensed copy?

**Your answer / rule forms attached:** ________________

- [ ] Not assessed (the factor stays TBD)


### B7. Weather (`weather`)

**Status: REQUIRES DOMAIN VALIDATION.** The software has no limit for this factor and reports it as *not evaluated*.

**Data we hold:** Only MET Malaysia marine and weather warnings, as free text that can cover several sea areas in one notice. A daily text forecast service exists and is a candidate source, but it is **not connected**. There is no hourly weather data.

**Questions:**

1. Which conditions matter (thunderstorms, heavy rain, low visibility, haze, lightning)?
2. Which of those are no-go, which caution?
3. How should a daily forecast be treated for a dive at a specific hour?

**Your answer / rule forms attached:** ________________

- [ ] Not assessed (the factor stays TBD)


### B8. Forecast uncertainty (`forecast_uncertainty`)

**Status: REQUIRES DOMAIN VALIDATION.** The software has no limit for this factor and reports it as *not evaluated*.

**Data we hold:** Very little. We record the data type (model or forecast), the retrieval time and the grid-cell distance. We do not hold ensemble spread, model run time or any provider uncertainty estimate.

**Questions:**

1. How far ahead is a forecast still usable for a go/no-go advice, per factor?
2. How old may the data be before it must be treated as missing?
3. Should a disagreement between two sources, or a marginal value close to a limit, always downgrade the advice? By how much margin?

**Your answer / rule forms attached:** ________________

- [ ] Not assessed (the factor stays TBD)


### B9. Site constraints (`site_constraint`)

**Status: REQUIRES DOMAIN VALIDATION.** The software has no limit for this factor and reports it as *not evaluated*.

**Data we hold:** Only one reference point: Pulau Tioman, from a public geocoder. No dive sites are registered. There are no stored site descriptions, exposure, hazards, entry points or local rules.

**Questions:**

1. Which dive sites do you want covered first? Please give each site's name and a source for its coordinates.
2. For each: exposure (which directions are open to swell and wind), entry/exit type, typical depth and any known current or surge hazards.
3. Local rules (permits, marine-park regulations, operator policies) the system must respect, with their source.

**Your answer / rule forms attached:** ________________

- [ ] Not assessed (the factor stays TBD)

## Part C: not asked, not evaluated

The software holds or may later hold data for these, but they are **not** risk factors with a
form, so they do not influence any recommendation today. If you think one should be, say so:

- Sea temperature (held as model data; not evaluated).
- Visibility, and depth or experience limits (no data, no rule).
- Marine-warning classification beyond decision A2.
