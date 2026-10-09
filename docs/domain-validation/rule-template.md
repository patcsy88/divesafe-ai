# Rule definition (one per limit)

Copy this form once for each limit you supply. Leave anything you do not know blank. **Do not
fill a field with a number you cannot source.** Nothing here is pre-filled on purpose.

```text
RULE ID:            (e.g. wave_height.shore.open_water; a short unique name)
RISK FACTOR:        (wind | wave_height | swell | swell_period | current | tidal_current |
                     weather | forecast_uncertainty | site_constraint)
STATUS:             REQUIRES DOMAIN VALIDATION        <- changed to VALIDATED only at sign-off

APPLIES TO
  Site(s):                      ____________________
  Dive type:                    shore / boat / drift / other: ______
  Diver qualification:          ____________________
  Maximum depth covered:        ____________________
  Seasons or conditions only:   ____________________

LIMITS  (write the unit; one line per level you want)
  NO-GO when:                   ____________________
  CAUTION when:                 ____________________
  GO is acceptable when:        ____________________   (all other rules and evidence also OK)
  Or describe the limit in your own words if it is not a simple band (conditional,
  combined with another factor, depends on a direction, ...):
                                ____________________
  Quantity the limit applies to (e.g. which kind of height, which direction convention):
                                ____________________

  Direction or exposure matters?  (e.g. depends on swell direction relative to the site)
                                ____________________

FORECAST MARGIN
  How much error must the system allow for because the input is a forecast or a model?
                                ____________________
  If the data are older than ____ or further ahead than ____, treat as: ______________

IF THE INPUT IS MISSING OR UNUSABLE
  The system will report "not evaluated" and "INSUFFICIENT EVIDENCE".
  Do you agree?  yes / no, because: ____________________

SOURCE  (required; a limit without a source cannot be used)
  Document / authority:         ____________________
  Version / date:               ____________________
  Page or section:              ____________________
  Is it written down publicly?  yes / no.  Where:  ____________________

CAN THE SOFTWARE APPLY THIS TODAY?   (the developer fills this in)
  Fields the software cannot represent yet (qualification, dive type, season, direction,
  per-factor age, margin):        ____________________
  Encoded narrower / refused:     ____________________

REVIEW
  Prepared by (name, role, qualification):    ____________________
  Date:                                        ____________________
  Independent second reviewer (REQUIRED for any limit that can allow GO or CAUTION):
                                               ____________________
  Review expires on (the rule must not be used after this date without re-review):
                                               ____________________

LIMITATIONS YOU WANT SHOWN TO THE USER
                                ____________________
```
