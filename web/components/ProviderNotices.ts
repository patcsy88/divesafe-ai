import { createElement as h, Fragment, type ReactElement } from "react";
import { plain } from "../lib/present.ts";

// ADR 0029 decision 11: the tides.today provider's own notice, shown whole beside the data.
// Written with createElement (no JSX) so that the offline tests can render it. Third-party text:
// plain text only (no link, no HTML, control and format characters removed), never cut. Not in a
// scroll box: a 2,000 character notice is shown in full. The two fixed sentences below are this
// app's own words, kept here; a safety test reads this file and pins the note equal to the API's
// `PROVIDER_NOTICE_NOTE` in `src/divesafe/api/schemas.py`. The "missing" sentence is never part of the provider's text:
// it is rendered once, outside the provider block, and alone when there is no provider text.
export const PROVIDER_NOTICE_TITLE = "Provider notice (tides.today)";
export const PROVIDER_NOTICE_NOTE =
  "Text supplied by the data provider, shown as received. It is the provider's own copyright and licence notice, shown because this assessment retrieved tides.today data (that data may have been withheld; see Evidence, if your key can read it). DiveSafe has not checked it. It does not validate the data, does not describe the tide at the dive site, and does not change the result.";
export const PROVIDER_NOTICE_MISSING_TEXT = "provider notice missing for a tides.today item";

export default function ProviderNotices({
  notices,
  notShown,
  missing,
}: {
  notices: readonly string[] | undefined;
  notShown?: number | undefined;
  missing?: boolean | undefined;
}): ReactElement | null {
  const list = Array.isArray(notices) ? notices : [];
  const noText = missing === true;
  if (list.length === 0 && !noText) return null;
  const more =
    typeof notShown === "number" && Number.isFinite(notShown) && notShown > 0
      ? Math.floor(notShown)
      : 0;
  const block =
    list.length > 0
      ? h(
          "section",
          { className: "card", "aria-labelledby": "provider-notice-heading", "data-testid": "provider-notices" },
          h("h2", { id: "provider-notice-heading" }, PROVIDER_NOTICE_TITLE),
          h("p", { className: "hint", role: "note" }, PROVIDER_NOTICE_NOTE),
          list.map((text, i) => h("pre", { key: i, className: "provider-notice-text" }, plain(String(text)))),
          more > 0 ? h("p", null, `${more} more provider notices not shown`) : null,
        )
      : null;
  const absent = noText ? h("p", { "data-testid": "provider-notice-missing" }, PROVIDER_NOTICE_MISSING_TEXT) : null;
  return h(Fragment, null, block, absent);
}
