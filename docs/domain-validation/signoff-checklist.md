# Sign-off checklist

A rule is only encoded as `VALIDATED` when every box below is ticked for it.

## Per rule

- [ ] Every limit has a unit and a named, dated source (not "common practice").
- [ ] The people and dives it covers are stated (qualification, dive type, depth, site).
- [ ] The unit AND the exact quantity are named (which height, which direction convention).
- [ ] A review expiry date is set.
- [ ] Forecast margin and data-age behaviour are stated.
- [ ] The behaviour with missing data is agreed ("not evaluated", `INSUFFICIENT EVIDENCE`).
- [ ] An independent second qualified person has reviewed it. **Required** for any limit that can
      allow `GO` or `CAUTION`; for a `NO-GO`-only limit, record why if there is none.
- [ ] You are comfortable with the system applying it **only as advice** that a human can override.

## Per factor you did NOT assess

- [ ] You have written "not assessed". The factor stays `TBD`; **while any factor is unassessed the
      software cannot say `GO` or `CAUTION`.**

## Part A decisions

- [ ] A1 to A5 in the questionnaire are each answered or explicitly deferred.

## Overall

- [ ] You understand the system **cannot guarantee safety** and is not a substitute for training,
      briefings or the dive leader's judgment.
- [ ] You have not been asked to approve any limit you do not personally stand behind.

```text
Name and role:
Qualifications / certifications:
Organisation:
Date:
Signature:
```

## For the developer receiving this

- Encode limits exactly as written; do not round, merge or "improve" them.
- Send the professional a **read-back table** (limit, unit, quantity, scope, as encoded) and get it
  confirmed in writing before marking anything `VALIDATED`. The signed document is the only
  evidence a rule is validated; the software cannot check it.
- A limit whose scope the code cannot represent is not encoded; say so on the form.
- The source becomes the rule's `citation`. Store reviewer and date with it.
- Run `risk-reviewer` and `security-reviewer`, add failure-condition tests, and mutation-check them.
- Do not mark a factor `VALIDATED` unless its rule is signed off here.
