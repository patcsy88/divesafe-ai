# Domain validation pack for a dive professional

DiveSafe AI cannot recommend `GO` for any dive until a qualified person supplies **sourced,
reviewed safety limits**. This pack is how that happens. It is written for a dive professional
(instructor, dive master, dive-centre safety officer, or a person who knows the local diving
practice), not for a programmer.

> **Signing a limit does not make any dive safe.** This pack feeds a decision-support tool. The
> diver, dive master or dive leader always makes the final decision.

## Why this exists

The software ships **no numeric safety limits**. Every limit is `TBD - REQUIRES DOMAIN
VALIDATION`, so today every assessment is `INSUFFICIENT EVIDENCE`. That is deliberate: a limit
written from memory by a developer or an AI is a safety risk. The numbers have to come from you,
with a source, and be reviewed.

## What we are asking of you

1. Read the [questionnaire](questionnaire.md). It has two parts:
   - **Part A:** five decisions about how cautious the system should be.
   - **Part B:** one short form per risk factor (nine in total).
2. For each factor, either supply a limit **with its source**, or write "not assessed", which is
   an honest answer. Please read "What your answers can and cannot unlock" below first: a factor
   you do not assess keeps the software from ever saying `GO` or `CAUTION`.
3. Fill in one [rule definition](rule-template.md) per limit you supply.
4. Complete the [sign-off checklist](signoff-checklist.md).

## Ground rules

- **Do not guess.** A blank is safe for the software (it stays cautious); a guessed number is not.
- **Cite everything.** A limit needs a named, dated source: a training-agency standard, a local
  operator's written rule, an official marine authority publication, or your own documented
  site briefing. "Common practice" is not a source; say whose practice and where it is written.
- **Limits depend on context.** Say who the limit is for (diver qualification), what kind of dive
  (shore or boat, depth, drift), and which site. See "What the software can apply today".
- **Say how much margin to leave for forecast error.** The data we hold are forecasts and model
  output, not measurements.
- **You decide; the software does not.** The system only advises. The diver, dive master or dive
  leader always makes the final decision, and can override the system with a written reason.

## What your answers can and cannot unlock

- **All nine factors need a signed-off rule** before the software can say `GO` or `CAUTION`. If
  you leave any factor unassessed, the answer stays `INSUFFICIENT EVIDENCE` however good the
  weather looks. That is the intended safe behaviour, but it means a partial pack cannot unlock a
  recommendation.
- **Four factors also need data we do not have.** Tidal current, weather, forecast uncertainty
  and site constraints cannot be encoded until a data source exists.
- **Decision A1 matters just as much.** Today the software refuses `GO` or `CAUTION` on the
  regional model data we hold, whatever the rules say.
- **So the realistic first result is more `NO-GO` and `INSUFFICIENT EVIDENCE`,** not `GO`. A `NO-GO`
  limit you supply can feed the decision support before everything else is in place.

## What the software can apply today

A signed-off limit is stored as a *rule definition* (see `docs/adr/0009-rule-definitions.md`).
Today the software **can** apply a limit that is scoped to named **sites** and a **maximum
depth**, written against a value it really holds, in that value's **unit**, with your **forecast
margin**, the **direction that is worse**, up to three bands (no-go, caution, go), your **source**,
reviewers and an **expiry date**. It applies the worst value over the dive window, including the nearest forecast sample on each
side of it, and fails closed after the expiry.

It **cannot yet** apply a limit that depends on the **diver's qualification**, the **dive type**
(shore, boat, drift), the **season**, the **direction a site is exposed to**, or a **per-factor
data age**. A limit that depends on any of these is recorded as such and **refused**; it is never
widened to cover cases you did not sign for.

Only five factors can be encoded now, because only they have data: wave height, swell, swell
period, current and wind (wind **speed** at 10 m; gusts and direction are recorded but cannot yet be
encoded). Tidal current, weather, forecast uncertainty and site constraints have no data source, so
no definition for them can be built until one exists. Please still answer for
them; your answers decide what data we must find.

## What we can and cannot give you

The system's data are coarse regional model output, not site measurements. The questionnaire
states, per factor, exactly what data exists, in what units, at what resolution and with what
known weaknesses. If the data cannot support a limit you would want to use, say so; that is a
valuable finding, not a failure.

## What happens next

1. A developer encodes each signed-off rule exactly as written, as a versioned rule with your
   source as its citation.
2. The risk reviewer and security reviewer check it, and tests prove it cannot be relaxed by the
   AI layer.
3. A factor becomes active only once its rule is marked `VALIDATED`. `GO` also needs the owner
   decision on degraded data (Part A, question A1).
4. Rules are re-reviewed when the source, the data provider or the site changes.

## Files

| File | Purpose |
| --- | --- |
| [questionnaire.md](questionnaire.md) | Decisions and per-factor questions, with the data we hold |
| [rule-template.md](rule-template.md) | The form for one limit |
| [signoff-checklist.md](signoff-checklist.md) | Completeness and sign-off |
