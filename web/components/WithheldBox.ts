import { createElement as h, type ReactElement } from "react";
import { capWithheld, clip, formatWhen, plain, provenanceLabel, shown } from "../lib/present.ts";
import type { EvidenceItem } from "../lib/types.ts";

// Written with createElement (no JSX) so that the offline tests can load and render it.
// Everything below is text: no link, no anchor, no DOM id taken from an item, no raw HTML.
export const WITHHELD_TITLE = "Withheld evidence - NOT used, NOT evaluated, may hold a more severe result";
export const WITHHELD_INTRO =
  "These items disagreed with other data under one id. They were not used and may hold a more severe result than the one shown.";

function item(it: EvidenceItem, n: number): ReactElement {
  const notes = Array.isArray(it.quality_notes) ? it.quality_notes : [];
  return h(
    "article",
    { key: n, "aria-label": "Withheld item" },
    h("h3", null, clip(it.category)),
    h(
      "p",
      null,
      h("span", { className: "tag" }, clip(it.data_kind)),
      h("span", { className: "tag" }, provenanceLabel(String(it.data_kind))),
      h("span", { className: "tag" }, "not evaluated"),
    ),
    h(
      "dl",
      null,
      h("dt", null, "Id (items sharing an id conflict)"),
      h("dd", null, clip(it.id)),
      h("dt", null, "Source"),
      h("dd", null, clip(it.source) + (it.source_version ? ` (${clip(it.source_version)})` : "")),
      h("dt", null, "Retrieved"),
      h("dd", null, formatWhen(it.retrieved_at)),
      h("dt", null, "Refers to"),
      h("dd", null, formatWhen(it.valid_at) + (it.valid_until ? ` to ${formatWhen(it.valid_until)}` : "")),
    ),
    notes.length > 0 ? h("ul", null, notes.map((q, i) => h("li", { key: i }, clip(q)))) : null,
    h(
      "details",
      null,
      h("summary", null, "Raw values (third-party text, shown as plain text)"),
      h("pre", { className: "value" }, shown(it.value)),
    ),
  );
}

export default function WithheldBox({
  items,
  note,
}: {
  items: readonly EvidenceItem[] | undefined;
  note: string | undefined;
}): ReactElement | null {
  if (!items || items.length === 0) return null;
  const { shown: list, hidden } = capWithheld(items);
  return h(
    "section",
    { className: "card warn", "aria-labelledby": "withheld-heading", "data-testid": "withheld-evidence" },
    h("h2", { id: "withheld-heading" }, WITHHELD_TITLE),
    h("p", null, WITHHELD_INTRO),
    h("p", { role: "note" }, plain(note ?? "")),
    list.map(item),
    hidden > 0 ? h("p", null, `${hidden} more withheld items not shown`) : null,
  );
}
