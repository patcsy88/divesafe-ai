# ADR 0005: Marine warnings need human reading

- Status: Accepted
- Date: 2026-10-09

## Context

MET Malaysia warnings arrive through data.gov.my as free text. One warning can name several
regions in different paragraphs, there is no structured location field, and some entries (for
example a tropical-cyclone "No Advisory" notice) have no validity window. Parsing text to decide
"does this apply to Pulau Redang?" would be an unreviewed heuristic whose failure mode is
silently ignoring a real warning.

## Decision

The connector never decides applicability. A deterministic rule,
`WarningNeedsHumanReadingRule`, returns `INSUFFICIENT EVIDENCE` whenever a `warning` or an
`advisory_without_validity` item is present for the dive window. The rationale cites the evidence
IDs. The recommendation can only be relaxed by the human (ADR 0004) after reading the text.

`no_active_warnings` (the feed returned nothing overlapping the window within a 30-day issue
lookback) does not trigger the rule, but it is worded as "no warning found in this feed", not as
proof of calm.

## Consequences

- Automatic `GO` is unreachable while any warning or undated advisory is present. Because
  undated notices such as "No Advisory" appear regularly, `GO` will be uncommon until a reviewed
  classification of MET Malaysia notice types exists. This is intentionally conservative.
- The rule has no numeric thresholds and so needs no threshold source; its citation is this ADR.
- Removing the over-blocking requires a verified mapping of notice types and regions, which is
  tracked in docs/risk-model.md and docs/data-sources.md.
