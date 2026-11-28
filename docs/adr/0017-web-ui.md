# ADR 0017: Web UI (Next.js)

- Status: Accepted
- Date: 2026-10-11

## Context

ADR 0011 planned a web interface that depends only on the `/v1` API. The API takes the actor from
credentials, so the UI must not collapse people into one identity.

## Decision

- `web/` is a Next.js 16 + React 19 + TypeScript app with no other runtime dependency. Versions are
  pinned exactly; `npm audit --omit=dev` reported none at the time.
- **Sign-in is a personal API key.** The Next.js server seals it with AES-256-GCM (key derived by
  HKDF from `DIVESAFE_WEB_SESSION_SECRET`, at least 32 characters) into an httpOnly, SameSite=Strict
  cookie (Secure in production) that expires after 8 hours. Browser code never sees the key. The API
  still checks the key on every call; the cookie grants nothing by itself.
- Server actions and server components make every API call. `DIVESAFE_API_URL` must be https, or
  http on loopback. Redirects are refused, calls time out, and error text is mapped to fixed messages. Only 422 validation text is shown (truncated, plain text).
- **No risk logic.** The UI shows `final_recommendation` as returned. An unrecognised value is shown
  as INSUFFICIENT EVIDENCE, never as safe. PENDING HUMAN is shown prominently. Model-written text is
  rendered as plain text inside a box labelled unverified. Times need an explicit zone.
- A CSP, frame denial, `nosniff`, no referrer and `no-store` are set on every response.
- `web/tests/boundary.test.ts` fails if a client component imports a server module or reads the
  environment, if `NEXT_PUBLIC` is used, if provider names or limit-like comparisons appear, or if
  the code logs or injects HTML. Seven guards were mutation-checked in an earlier pass.

## Pages

Sign in; sites list (`GET /v1/sites`, added to the API, viewer role); run an assessment; the result
with the decision form (assigned decider only, enforced by the API); the evidence view; the
actual-conditions form (shown once the planned start has passed).

- **The UI never decides what an override is.** The decision form lists outcomes most severe first,
  preselects nothing, and shows a prominent warning when the choice is less severe than the system's.
  That comparison is display only. The API derives and records the override, and requires a reason.
- The form shows the system recommendation, rules alone, the count of source issues and unevaluated
  factors, and links to the evidence before the choice.
- On a 409 from a write, the action goes to the record only after a fresh read shows it was recorded.
  Otherwise it shows the API's 409 text, stripped of control and bidi characters and cut at 300
  characters, or a fixed sentence when the API sent none.
- Evidence items are labelled by `data_kind`: only an observation is "measured". Notices and
  knowledge are "text, not a measurement". Quality tags are not sufficiency, and the page says so.
- **Withheld evidence box.** When the evidence response carries `withheld_evidence` (items dropped
  because two sources disagreed under one id, ADR 0008), the evidence page shows a separate,
  labelled box above the evidence list: "Withheld evidence - NOT used in the main result, checked
  separately for a more severe result", the server's `withheld_note`, then each item. Everything is
  plain text: no link, no anchor, no DOM id from an item, no raw HTML; ids are shown as text; raw
  values sit in a collapsed block through the same `shown()` helper (cut at 20,000 characters). At
  most 50 items are shown, ordered by id, followed by "N more withheld items not shown". Source,
  source version, category and each quality note are cut at 1,000 characters (the normal evidence
  list is not).
  An older response without
  the field renders nothing. (Not to be confused with `DECISION_WITHHELD`; see the terms block in
  [architecture.md](../architecture.md).)
- Actual conditions are free reports. Nothing is compared to a limit. Sea temperature may be
  negative; other values may not.

## Review changes (security-reviewer, 2026-10-11)

Fixed: a revoked key clears the session via `/logout`; unknown status is treated as pending; a
recorded human decision, override flag and rationale are shown; failed writes with an unknown
outcome no longer say "nothing was recorded"; control and bidi characters are stripped from
third-party text; the id pattern matches the API's; extra headers, `object-src 'none'` and
`images.unoptimized`.

## Retry keys and confirmation

- The forms send an `Idempotency-Key` (a random 32-hex value made in the browser after first
  render, new whenever any field changes or the API answers 409). Without `crypto` no key is sent.
  A decision that differs from the one on record is not treated as recorded: the person is told
  theirs was not recorded. See ADR 0011 for the API behaviour.
- The decision form shows the outcome, the system's recommendation and the reason, and records
  only on a second, explicit "Confirm and record" press. Editing anything returns to the review.

## Known gaps

- **Login throttle is per instance.** The login action allows 5 rejected keys a minute per source
  (in memory, one instance; replicas multiply it, restarts reset it), below the API's 10, and the
  API counts failures too (ADR 0013). Each attempt is reserved before the network call. Behind this
  server the API sees one address unless the web server is listed in `DIVESAFE_TRUSTED_PROXIES`
  and `DIVESAFE_WEB_TRUST_PROXY=1` is set; without both, all sign-ins share one bucket and one
  person can block everyone for a minute (nothing is recorded; assessments stay pending). With
  `TRUST_PROXY=1` the web app must be reachable only through a proxy that overwrites or appends
  `X-Forwarded-For`. Only sign-in forwards the address, so a 401 elsewhere (a stale session) counts
  against the web server's own bucket when it is not trusted. Reservations of in-flight successful
  sign-ins count briefly, so five simultaneous sign-ins from one bucket can see a 429 (nothing is
  recorded). The API timeout is 30 s (`API_TIMEOUT_MS`; this line said 120 s until 2026-11-23),
  shorter than the 60 s window. Whether a late release can still remove another attempt's hit has
  not been re-checked against 30 s. `login` is a `"use server"` action with no direct test; the
  library is tested.
  API keys must be generated (at least 128 bits), as the hashes are unsalted.
- A retry key covers an unknown-outcome retry only while the form is unchanged and the page stays open; reloading starts a new attempt, so a timed-out create can still be duplicated after a reload. Check the record list before retrying.
- No "who am I" endpoint, so the UI cannot hide actions a role cannot perform. Sign-in calls `GET /v1/sites`
  and accepts a 403 only when the API's own refusal text is present.
- The cookie is the equivalent of the key for 8 hours. It cannot be revoked server-side; revoke the
  key at the API. One leaked session secret decrypts every stolen cookie; there is no rotation or
  version byte, and only length is checked.
- The CSP still allows inline scripts. A per-request nonce with `strict-dynamic` via `proxy.ts` is
  feasible (every page is dynamic) and not done. No HSTS, no `__Host-` cookie prefix, and `Secure`
  follows `NODE_ENV`. Behind a proxy, `serverActions.allowedOrigins` must be set; Next lets a
  request without an `Origin` header through. (Caddy amendment 2026-11-24, built in code: HSTS,
  the `__Host-` prefix and an always-`Secure` cookie; `allowedOrigins` stays unset because Caddy
  fixes `X-Forwarded-Host`. The inline-script CSP stays, R8.)
- API responses are cast, not validated, and not size-capped.
- The boundary test is a tripwire: it misses extension-less imports, transitive imports and many
  sinks. No tests for headers, cookie attributes or the action origin check.
- CI (`web` job: `npm audit --omit=dev --package-lock-only`, a check that no locked package has an install script, `npm ci --ignore-scripts`, typecheck, tests, build on Node 24; see ADR 0024 "CI and supply chain"). It ran green on GitHub from run #62 (2026-11-06). A new advisory can fail an unrelated change. Not yet run in a container or behind a proxy. Tested against a
  fixture-backed API only, not a deployed one.

## Review changes (risk and security review of the decision pages, 2026-10-11)

Fixed: notices were labelled "measured"; the less-severe override is now flagged on the form and the
decision card; the form shows context before the choice; the conditions form waits for the planned
start; echoed 409 and 422 text is stripped of control and bidi characters; sign-in no longer trusts a
bare 403 (e.g. from a proxy); timeout wording no longer tells a decider to "check for a new
assessment"; the repository scan exclusion is anchored to `web/node_modules` and `web/.next`.

Gaps added:
- A form loses its typed text after an error. The decision form now has a review-and-confirm step
  (display only; the API still derives the override and demands the reason). Safety rests on the
  API's write-once compare-and-swap, not on the confirm step.
- Responses are cast, not validated, and not size-capped; evidence values are truncated to 20,000
  characters on screen only. One malformed item could still crash a page.
- The backend `observations` object is open-keyed and unbounded in value; bidi and C1 characters are
  rejected by the UI but stored raw if sent to the API directly. `notes` is untrusted human text and
  must be delimited if it is ever used in a prompt, the corpus or history.
- The evidence page does not show the planned dive window against each item's validity.
- **Withheld box gaps.** Web tests are source-level checks plus one offline render test
  (`web/tests/withheld.test.ts` renders `WithheldBox` with `renderToStaticMarkup`); there is no
  browser test, and the page itself is checked by reading its source. `plain()` strips control and
  bidi characters but leaves lookalike letters, combining marks and U+2028/U+2029. The withheld
  items' dates have no "after assessment" marker.
- The web actions themselves have no tests (ID tampering, a 401 mid-write). Only the library is tested.

## Amendment 2026-11-22: restyle with HeroUI v3 (ACCEPTED)

- Status: ACCEPTED (owner, 2026-11-22). Owner decisions 2026-11-22: the web UI stays a browser app;
  it is restyled with HeroUI v3; no other HeroUI product is used. Not deployed anywhere yet, so the
  deploy items (session secret, TLS proxy, `allowedOrigins`, trusted proxies, a compose web service,
  the gaps above) are separate and unchanged by this amendment. Accepting this amendment approves
  the design only. Every "before merge" and "before acceptance" check below applies to the restyle
  PR(s), and none has been done yet. A restyle PR merges only after each check is met and recorded
  in it: the dependency and licence checks, the CSP and header check on the production build, the
  tests (including the badge-class test before `lib/present.ts` changes), the ADR 0008 Amendment 1
  (a) repeat with the text and role comparison, and the screenshots. It also needs `risk-reviewer`
  and `security-reviewer`.
- Changes only how `web/` looks, and replaces "no other runtime dependency" (Decision, first item)
  with the packages listed under "New dependencies". Nothing else in "Decision" is relaxed.

### Decision

Restyle `web/` with HeroUI v3 (`@heroui/react` and `@heroui/styles`, line 3.2.x, open source), which
sits on React Aria Components and Tailwind CSS v4, needs no provider and animates with CSS only.
Reasons: accessible components (roles, labels, focus handled by React Aria) instead of more
hand-written CSS, and one consistent look for the forms, tables and badges.

Rejected:
- **Ionic (and Capacitor, a native app):** mobile-first and client-rendered. It fights the
  server-rendered pages and the rule that only the server holds the API key.
- **Keep the hand-written `globals.css`:** safe, but every control's accessibility and focus
  behaviour stays our own to maintain.
- **HeroUI Pro, Chat, Agents, MCP:** not used, in the app, the build, or any agent or editor
  configuration in the repository (`opencode.jsonc`, `.opencode/`). Checked before merge: the lock
  file has no `@heroui/*` package other than `@heroui/react`, `@heroui/styles` and their
  dependencies; every `resolved` is on `registry.npmjs.org`; no `.npmrc` is added.

### New dependencies

Exact-pinned like the rest of `package.json`, all under `dependencies` so that `npm audit
--omit=dev` covers them: `@heroui/react` and `@heroui/styles` (3.2.x, Apache-2.0), `tailwindcss` and
`@tailwindcss/postcss` (4.x, MIT), and `@heroui/react`'s peer dependencies, exact-pinned here too so
they never float within the peer range: `react-aria`, `react-aria-components`, `@react-aria/ssr`,
`@react-aria/utils` and `@internationalized/date` (Apache-2.0), each at a version inside
`@heroui/react`'s declared range (`tailwindcss`, also a peer, is already listed). `npm ci` must
succeed without `--legacy-peer-deps` or `--force`, so a version outside a peer range fails the
install instead of being accepted. `@heroui/react`'s own dependencies (`@radix-ui/react-avatar`,
`input-otp`, `tailwind-variants`, `@react-stately/utils`, `@react-types/*`), `@heroui/styles`'s
(`tw-animate-css`, `tailwind-variants`) and Tailwind's native engines `@tailwindcss/oxide` and
`lightningcss` (MPL-2.0, a build-time tool) stay transitive, fixed by the lock file. A HeroUI bump
that adds or widens a peer dependency is reviewed like a new direct package. Any other direct
package needs a new security review. CI's existing `web` job checks advisories and install scripts
only (`npm audit --omit=dev --package-lock-only`, the no-install-script check, `npm ci
--ignore-scripts`); a new advisory can fail an unrelated change (already a known gap). Build-time
packages (Tailwind's native engines, the PostCSS plugin and its loaders such as `jiti`) run code
during `next build`, which `--ignore-scripts` does not cover, so the build runs with no secret in
the environment (no `DIVESAFE_WEB_SESSION_SECRET`, no API key). To do before merge: on the real lock
file, record the licence of each direct package and of React Aria, and any licence among the bundled
packages that is not MIT, Apache-2.0 or ISC; record any licence other than MIT, Apache-2.0 or ISC
among build-time packages too; note that the published 3.2.x packages' `LICENSE` and `package.json`
(Apache-2.0) govern, not the repository root's older MIT file; record whether any bundled Apache-2.0
package ships a `NOTICE` file and, if so, where its text is kept; confirm no `hasInstallScript`, and
that the lock file holds the Linux builds of the native engines (`@tailwindcss/oxide`,
`lightningcss`) as well as the macOS ones. Licences are read from each package's licence file as
well as the lock file's `license` field, and a mismatch is recorded. Do not accept a package that
adds an install script. Done in restyle step 9 (2026-11-23): the results are in restyle PR #62,
"Dependency record".

Found on the real lock file (2026-11-22): every `@heroui/react` 3.2.x release pins
`@react-types/color` 3.2.0, which depends on `@react-spectrum/color` and through it on
`@adobe/react-spectrum` and its icon packages. They are Apache-2.0, except `react-transition-group`
(BSD-3-Clause). The installed `@adobe/react-spectrum-workflow` 2.3.5 (pinned by
`@spectrum-icons/workflow` 4.3.2) is Apache-2.0 in its `package.json`, its `LICENSE` file and the
lock file; its later release 2.4.0 is CC-BY-ND-4.0, so a change that moves it to 2.4.0 or later is
refused unless the owner accepts that licence (`web/tests/spectrum.test.ts` fails if the lock file
moves it from 2.3.5 or holds any `-ND` or `-NC` licence). HeroUI uses `@react-types/color` for
TypeScript types only, and no runtime file in `@heroui/*`, `react-aria` or `react-aria-components`
imports these packages. So they are installed but must not ship or run. No code in `web/` imports
them, and a web test fails if any file in `web/` imports `@adobe/`, `@react-spectrum/`,
`@spectrum-icons/`, `react-transition-group` or `@react-types/color`, type-only imports included.
Before merge, on the production build: no file in the build output (browser and server) contains
code from `@adobe/*`, `@react-spectrum/*`, `@spectrum-icons/*` or `react-transition-group`. This is
checked by module path (the server's `.nft.json` trace files and source maps, and the `sources` of a
scratch build with `productionBrowserSourceMaps: true`, not committed) and by string literals that
survive minification (listed in `check-no-spectrum.mjs`: two from `@adobe/react-spectrum`, which
every `@spectrum-icons` and `@react-spectrum` package loads, and one from `react-transition-group`).
The icon packages `@adobe/react-spectrum-workflow` and `-ui` have no string of their own; they ship
only through `@spectrum-icons` (caught by the strings) or a direct import (caught by the web test).
The check was first shown to find deliberate imports in a scratch build (2026-11-22: a workflow
icon, `@adobe/react-spectrum`, `react-transition-group` and `@react-spectrum/provider` from a client
file; a server-file import fails `next build` and is also found). The restyle PR does not merge if
any is found. A HeroUI, `@react-types/color` or `@spectrum-icons/workflow` change that alters this
chain is reviewed like a new direct package. When a web image is built, it copies only Next's traced
output (`output: "standalone"`), never the whole `node_modules`. CI's `web` job repeats the check on
every change, after `next build` (`web/scripts/check-no-spectrum.mjs`, owner, 2026-11-22): it fails
if any `.nft.json` trace file or source map lists a file in those four package scopes, if any built
`.js` or `.css` file holds one of the recorded strings, if any file under `.next` is itself in one
of those package scopes, or if it finds nothing to check. Known gap: CI's build has no browser
source maps, so in CI the browser output is checked by string only.

HeroUI's stylesheet applies to every page, and a class of ours that it also defines takes on every
property our rule does not set (found 2026-11-22: `badge`, `card`, `label`, `link` and `tag`; the
result badge got HeroUI's grey background). So every class in `app/` and `components/` carries the
`ds-` prefix, and `web/tests/classes.test.ts` fails if a class is used without a rule in
`globals.css`, has no prefix, is set from anything but a literal or `badgeClass(...)`, is set
through a quoted `className` key or a `class` prop, or is also a class in `@heroui/styles`. HeroUI's
rules that are not tied to a class are Tailwind's element reset, a border colour on every element
and two `:root` settings; its other unclassed selectors need attributes the app does not use
(`data-slot`, `data-theme`, `data-reduce-motion`, `data-scrollbar`, `dir`). The reset removes
paragraph and list spacing, list bullets, heading weight and link underlines, so `globals.css` puts
these browser defaults back with element rules. On 2026-11-22 every element of the result, evidence,
home and login pages had the same computed style as at `fbad5a2`, in light and dark mode and with
reduced motion on and off (in Chromium, as first rendered; the decision form's less-severe warning,
the confirm step and the error alerts were not opened and are left to the (a) repeat, and were
checked in restyle steps 7 and 9). The same holds for custom properties: HeroUI also defines
`--accent`, so ours carry the `--ds-` prefix, and `classes.test.ts` fails on an unprefixed one or
one HeroUI also defines. HeroUI's dark theme variables switch only on a `.dark` class or
`data-theme="dark"`, which would need a script, a cookie or stored state (security N3, round 1),
so `globals.css` sets HeroUI's own dark variable values
under `@media (prefers-color-scheme: dark)`, and `classes.test.ts` fails unless they equal the
values in `@heroui/styles` that differ from its light theme, each set once and nowhere else (on
2026-11-22 all 31 resolved in Chromium to the same values as HeroUI's `.dark`). That covers dark
mode only while HeroUI keys nothing else on `.dark` or `data-theme`, so the test also fails if a
HeroUI selector naming them is not one of the eight listed in it (the theme blocks, the unused
vibrant palette and the `dark` variant), if `@heroui/styles` names a stylesheet other than its
`index.css`, if HeroUI's `.dark` sets anything but variables and `color-scheme`, if `globals.css`
drops `color-scheme: light dark`, reads a variable that nothing sets or reads a HeroUI one outside
the dark block, or if a file in `app/`, `components/` or `lib/` sets a theme class or attribute,
uses a `dark:` utility, or names browser storage, `document.cookie` or a theme provider. HeroUI's
`dark` variant has a `prefers-color-scheme` branch, but it also needs the utility's class on an
ancestor, so a `dark:` utility did not apply in system dark mode (Chromium, 2026-11-22, a scratch
build with no `.dark` anywhere); dark styles therefore go in the `globals.css` media block, and the
test also fails if any CSS or JavaScript file in `@heroui/styles` or `@heroui/react` uses `dark:`
outside a comment. Focus rings and other interaction states take HeroUI's dark values too, so the
(a) repeat includes a keyboard pass through the decision form and its confirm step in dark mode.
HeroUI's own colours fail contrast against ours (white on its accent 3.59:1; in dark mode its field
background against our card 1.09:1, with no field border), so ten HeroUI variables are set from our
`--ds-` palette in both modes (option A1, owner, 2026-11-22): `--foreground`, `--background`,
`--border`, `--accent`, `--accent-foreground`, `--focus`, `--field-background`,
`--field-foreground`, `--field-border` and `--field-border-width` (`1px`). The field outline is
therefore as at `fbad5a2`, below 3:1 against the card: not a regression, and not fixed by A1. The
dark block then holds HeroUI's own dark value for every other variable (values HeroUI builds from a
mapped variable, such as `--accent-soft`, now resolve from our palette, so they are not HeroUI's
tested colours); `classes.test.ts` fails if the ten change, if another HeroUI variable is set, or if
a set one is not defined by HeroUI. HeroUI components (`Button`, `Input`, `Label`, `TextArea`) are
used only in `"use client"` files, imported by subpath from a fixed list in that test, which also
refuses any other import of `@heroui/`, `react-aria` or `@react-aria/` and `@react-stately/`
packages; `isRequired` (adds a visible `*`), `isPending` and `Description` (renders nothing outside
a field) are refused, and native `<select>` stays. Our rules whose selector starts with `button`,
`input`, `label` or `textarea` apply only to elements without `data-slot`, and the test fails on one
that does not (a descendant rule such as `.ds-row input` is not checked). Text in fields stays 16px
(`.ds-field`). React Aria adds one `<style>` element to the page head (`touch-action` on pressable
elements), which the current `style-src 'unsafe-inline'` allows; a nonce CSP would have to account
for it. In forced-colours mode, where HeroUI's box-shadow focus ring is removed, its components get
a solid outline. Tailwind scans only `app/`, `components/` and `lib/` (`source(none)` and
`@source`), so text in tests or scripts never reaches the shipped CSS. CI also runs
`web/scripts/check-built-css.mjs` after the build (owner, 2026-11-22): it fails if a built
stylesheet keeps an `@import`, names an `http(s)` or protocol-relative address, has a `url()` or
`image-set()` address that is not a `data:` URI or a `/_next/static/` file (a `..` step, an encoded
dot or a backslash counts as leaving it), or if the build makes no stylesheet or more than one
(owner, 2026-11-23: a second would hold CSS the source checks never read; a Next change that
splits the CSS is reviewed before the rule changes, not met by raising the limit). `globals.css`
imports HeroUI selectively, in its documented order (restyle step 8): `base/base.css`, the default
theme, `utilities`, `variants` and the component files of the components in that test's list
(`label`, `button`, `input`, `textarea`), not `@heroui/styles` as a whole and not `tw-animate-css`.
The built stylesheet went from 476 KB to 70 KB (45 KB to 9 KB gzipped), with every computed style
and screenshot unchanged. `classes.test.ts` fails unless those imports are exactly that base list
plus one file per listed component, in HeroUI's order and layers, each resolving through the
package's `exports` to a `dist/` file the checks read, with every file those import being a
relative `dist/` file the checks read too, and the files loaded are an exact list; `globals.css`
is the only stylesheet in `app/`, `components/` and `lib/` (owner, 2026-11-23). The class and
dark-selector checks still read every file in `@heroui/styles`.
`tw-animate-css` stays installed as a dependency of `@heroui/styles` but is not imported: importing
it again brings back the animation utilities the hiding rule bans, so it needs `risk-reviewer`
review, and an added overlay's enter and exit animations are re-checked under the (a) hiding rule.
React Aria's overlay code (`ariaHideOutside`, which sets `aria-hidden` or `inert` on everything
outside an open overlay, the banner, badge and note included) is reachable from HeroUI's `Input` and
is in the built browser chunks (one chunk of a local build held its `[data-react-aria-top-layer]`
walker, 2026-11-23); it runs only inside a combo box or popover, which the app does not render.
Adding any component therefore also re-runs the after-hydration attribute check (`aria-hidden`,
`inert`, `hidden`) on every page, with that component open.
`web/tests/hiding.test.ts` (owner, 2026-11-23: the three attributes, then the wider scope below)
refuses `aria-hidden`, `inert`, `hidden` and `popover` in the app's own source, with attribute and
tag names in any letter case: as JSX attributes, `createElement` props keys (shorthand, quoted and
computed ones included), property writes (dotted or by name, plain, compound, `++`/`--` or as a
destructuring target, or through `Object.assign`, `Reflect.set`, `defineProperty` or
`defineProperties`), or as quoted strings anywhere (a form field's `type="hidden"` aside), so a name
held in a variable is refused too. `\u` escapes and `\x` letter escapes are refused, and every write
through a computed name (`obj[key] =`, in any of those forms) is pinned by file. Every props
argument is `null` or an object literal written where the element is built, so props made elsewhere
never reach an element; spreads into props, `cloneElement` and the JSX runtime are refused, and a
capitalised tag must be a component the file imports or declares; the closed elements' names never
appear as strings, and `"details"` only as the tag of an `h()` call, so a tag name kept in a
constant elsewhere is refused. It also refuses `setAttribute`, `removeAttribute`, HTML writes,
`eval`, `Function` and code built from strings, `<dialog>`, `<template>`, `<noscript>`,
`<datalist>`, `<svg>`, inline styles, `role` none or presentation, `classList` and `className`
writes, imports from `tests/`, and removing text (`textContent`, `innerText`, `outerText`,
`nodeValue`, `.data`, `replaceChildren`, `replaceWith`, `removeChild`, `.remove()`, `.open =`); it
pins every `aria-label` and `aria-labelledby` and every `<details>`, by file, and pins the whole
text of the two that exist (owner, 2026-11-23), so a variable, child component, attribute or comment
added inside one fails. `lib/` builds no elements and does not look up the page (`document`,
`window`, `querySelector`); it can still change an element it is given, which the write checks
cover. Every script file under `web/` outside `app/`, `components/`, `lib/` and `tests/` is on a
reviewed list, and none of those folders may be a link. Classes are `classes.test.ts`'s: only `ds-`
classes styled in `globals.css`, so no Tailwind hiding utility. It reads the source as text, not as
a parsed program, so it is a tripwire, not proof: a name built at run time is not seen, nor is what
a HeroUI or React Aria component adds. A failure on text that is not an attribute is fixed by
rewording the text, not by loosening a pattern. Not checked (Known gaps, owner, 2026-11-23):
`aria-live`, `aria-busy` and other live-region attributes; hiding declarations in `globals.css`
itself (`classes.test.ts` checks class names, not what a `ds-` rule does); and a component or
function already used inside a `<details>` (today only `shown()`, which returns text) changing what
it renders or returns; the (a) harness opens every `<details>` before measuring, so it would not see
this either. Adding the test changed `web/`, so it ended the `web-baseline-20261123` deploy
baseline; the baseline moved to `web-baseline-20261123-2` on the build comparison recorded in ADR
0008 Amendment 1 (a). The checklist is never narrowed to skip `web/tests/`.

### What must not change

- No visible text changes, and no element that states a status, warning, limit or caveat is removed,
  merged, reordered or given a weaker role. Only styling and markup change. The lists below name the
  most important elements; they are not exhaustive. Among them, besides those named below: the run
  states ("NO RESULT YET", "Did not finish"), the decision-withheld box, the not-evaluated list
  ("not the same as being safe"), the source-failure and model-failure cards, the
  unverified-proposal box, the less-severe override wording on a recorded decision, the decision
  form's order (most severe first, nothing preselected, the less-severe warning, the confirm step),
  the conflicting-signals box with its caveat, and the evidence page's "measured" and quality
  wording.
- Pages stay server-rendered. Only the server talks to the API. The API key never reaches the
  browser. The sealed httpOnly cookie, the session and the login throttle are unchanged.
- No risk logic in `web/`. `final_recommendation` is shown as returned; an unrecognised value is
  INSUFFICIENT EVIDENCE.
- Model-written text is plain text, under a model heading, in the box labelled unverified. No
  `dangerouslySetInnerHTML`; no component that renders model or provider text as markup.
- **CSP and headers:** the CSP stays as it is: no new script, style, font or connect source, no CDN,
  no remote fonts, no runtime CSS-in-JS that needs `eval`. Tailwind output is built CSS served from
  `'self'`; fonts stay the system stack. This is verified on the production build, not assumed: `git
  diff fbad5a2 -- web/next.config.ts` shows no CSP line; the `Content-Security-Policy` header on
  `/`, an assessment page and the sign-in page equals the one at `fbad5a2`; the built CSS
  (`.next/static/css`) contains no `@import` of a URL, no remote `url(` and no `@font-face` with a
  remote source; `next/font/google` is not used; and the browser console shows no violation with
  every interactive component opened at least once (the decision form's choice and confirm step, any
  menu, select or dialog). The other security headers and `robots: noindex` are unchanged.
- **Meaning of the visuals:** each result keeps a distinct colour AND its text label (colour is
  never the only signal), and INSUFFICIENT EVIDENCE stays visibly different from GO. Kept as they
  are: the PENDING-human banner, the disclaimer (`role="note"`), the DEGRADED note right after the
  result and in the decision form's context, model output under its heading, provider notices, the
  withheld evidence box and the "a report, not a measurement by the system" wording on reported
  conditions.
- No safety text (banner, disclaimer, DEGRADED note, pending and withheld states, not-evaluated
  list, run states, override warnings) sits in anything collapsed, hover-only, dismissible or timed.
  All four result badges have the same size and weight. INSUFFICIENT EVIDENCE and NO-GO never use
  the success (green) style, and INSUFFICIENT EVIDENCE is never shown as disabled. The DEGRADED note
  stays a full-width block right after the badge, at least as prominent as now, not a small caption.
- Accessibility at least as good as now: roles (`alert`, `status`, `note`), labels on every input,
  visible focus, keyboard use of the decision form and its confirm step, contrast of at least WCAG
  AA in light and dark (4.5:1 for text; 3:1 for large text and the badge border), checked by hand on
  the screenshots. Measured on 2026-11-22 against the card background: light GO 5.43, CAUTION 4.40,
  NO-GO 7.33, INSUFFICIENT EVIDENCE 6.43; dark GO 3.00, CAUTION 3.70, NO-GO 2.22, INSUFFICIENT
  EVIDENCE 2.53. The restyle must bring every badge to at least 3.1:1 in both modes, and the badge
  label stays large text (at least 14pt bold), or else reaches 4.5:1.

### Client boundary

Reports exist of "client-only" errors when `@heroui/react` is imported in server components on Next
16. Rule: HeroUI interactive components are imported only in files that start with `"use client"`.
Server pages use plain elements with `@heroui/styles` classes, or thin client wrappers that receive
display data only: the strings, numbers and booleans the wrapper renders, already passed through
`plain()`, never a whole `view`, `record` or decision object, because every prop is serialised into
the page for the browser; never the key, the session or a server module. `DecisionForm` and
`ConditionsForm` keep their current props and server actions, and the restyle adds no new prop to
them. A wrapper for the DEGRADED note receives only whether to show it (a boolean from
`showsDegradedNote`) and renders `DEGRADED_EVIDENCE_NOTE` itself, imported from
`components/DegradedEvidenceNote.ts` (never a copy of the text). The existing boundary test (no
client file imports `lib/api|auth|config|session`, reads `process.env`, or uses `NEXT_PUBLIC`)
applies to the wrappers.

### Tests

`web/tests/degradednote.test.ts` and others read page source (for instance the order of the badge
and `<DegradedEvidenceNote>` in the page, and `className="pending"`). A restyle may force edits to
them. Any such change must keep or strengthen what the test proves, never weaken it, and needs owner
approval and `risk-reviewer` review (AGENTS.md section 4). `boundary.test.ts` is unchanged or
strengthened (for instance, to treat `'use client'` and a directive after a comment as client
files), never weakened. `DegradedEvidenceNote.ts`, `ProviderNotices.ts` and `NOTHING_RECORDED_503`
in `lib/api.ts` keep their paths, constants and offline renderability, so the Python safety tests
that read them keep passing unchanged. If the restyle needs to change any of them, the Python safety
test change follows AGENTS.md section 4 (owner approval, `risk-reviewer`, mutation check) like the
web tests. Mutation-check any new or edited guard. An edited `degradednote.test.ts` must fail on
each of these mutants: the note moved away from the badge, the note dropped from the decision-form
context, the note rendered unconditionally, and the note's role removed. A render of the result
block is preferred to a source-position check. Before the restyle touches `lib/present.ts`, a web
test pins that GO, CAUTION, NO-GO and INSUFFICIENT EVIDENCE map to four distinct badge classes, each
with its own label. If the restyle needs classes without the `ds-` prefix (Tailwind utilities, or a
`className` on a HeroUI component), `web/tests/classes.test.ts` gets an explicit allow-list, with
owner approval and `risk-reviewer` review. It still fails on any utility that hides, clips, shrinks,
moves off-screen or makes transparent (for example `hidden`, `invisible`, `sr-only`, `opacity-0`,
`collapse`, `h-0`, `w-0`, `size-0`, `max-h-0`, `scale-0`, `text-transparent`, `truncate`,
`line-clamp-*`, `-translate-x-full`, `animate-out`, or `overflow-hidden` on a safety element), with
any variant prefix such as `md:`, `dark:` or `motion-reduce:`, on any arbitrary value or
arbitrary-property class with the same effect, and on HeroUI's `badge`, `card`, `label`, `link` and
`tag` as a class of ours.

### ADR 0008 Amendment 1, Open preconditions (a)

The restyle changes `web/` after `fbad5a2`, the build on which ADR 0008 Amendment 1, Open
preconditions (a), was checked (2026-11-22). That check must be repeated on the restyled build
before the restyle is accepted, by the method recorded there: views from the real
`AssessmentView.of`; marked GO and CAUTION records show the note after the badge and in the
decision-form context; unmarked GO, CAUTION and NO-GO records show neither. Checked in the
server-rendered HTML whether or not the note's component is a client component, plus one browser
look in light and dark. In the same run, for each synthetic view, the page's visible text (tags
stripped, whitespace normalised, visually hidden text excluded) is the same before and after the
restyle. The sequence of `alert`, `status` and `note` roles, each with its text, is the same. No
element that holds the banner, the disclaimer, the DEGRADED note or an `alert`, `status` or `note`
role, nor any ancestor of one, has a hiding class or attribute (`hidden`, `invisible`, `opacity-0`,
`sr-only`, `aria-hidden`, `inert`, `animate-out`, or an inline style that hides it (`display: none`,
`visibility: hidden`, `opacity: 0`, clipping or zero size), or an entry animation that starts
invisible), and no such element's text is cut short, checked at phone width: for each element that
holds the text (measured on the element itself unless its computed `display` is `inline` or
`contents`, then on its nearest block container, never on a further ancestor), `scrollWidth -
clientWidth ≤ 1` and `scrollHeight - clientHeight ≤ 1`, and its bounding box lies inside every
ancestor whose `overflow` is not `visible`. The browser look is taken with `prefers-reduced-motion`
both on and off. If the confirm step is a modal or popover, it shows the result, the DEGRADED
context sentence and the less-severe warning itself, and the hiding check is repeated in the browser
with it open. Any other added role or hidden text is listed in the PR for `risk-reviewer`. The same
holds for the evidence page and the decision form's review and confirm states. The result, with the
restyled commit, replaces the `fbad5a2` entry in ADR 0008 Amendment 1, Open preconditions (a). The
note's wording stays pinned to the API constant.

### Reviews

- `security-reviewer`: the dependencies and licences, the CSP on the build, the client boundary, and
  the lock-file diff summary (packages added, any `hasInstallScript`, any non-registry `resolved`).
- `risk-reviewer`: colours and labels, the note and banner, any test change.
- Before acceptance: screenshots, by hand, of each of the four result badges, the PENDING banner and
  the DEGRADED note, in light and dark, and also an unrecognised result value, the "NO RESULT YET"
  and "Did not finish" states, the decision form's less-severe warning and confirm step, the
  decision-withheld box, the evidence page's withheld box, and a narrow phone width, attached to the
  PR for `risk-reviewer` and the owner.
- Both before merge; blockers fixed first.

### Known gaps added

- Component-level behaviour is trusted to HeroUI and React Aria; web tests stay source-level plus
  offline renders, with no browser test of focus, contrast or the CSP.
- A large, fast-moving dependency tree (HeroUI v3 is a recent line) and exact pins mean frequent
  bumps; no hashed Python lock file (ADR 0024 open item); the npm lock file's integrity hashes are
  checked by `npm ci`, but nothing reviews what a version bump brings in.
- Tailwind's generated CSS is not reviewed line by line; visual meaning is checked by hand and by
  the tests above.
- The CSP still allows inline scripts and styles (gap above); the restyle does not close it and must
  not widen it. React Aria's inline style attributes mean a later nonce CSP keeps `style-src-attr
  'unsafe-inline'`; the restyle makes the style half of that gap harder to close. React Aria also
  injects one `<style>` element into `<head>` at runtime (a fixed `touch-action` rule); a nonce CSP
  without `'unsafe-inline'` in `style-src-elem` must allow it.

## Amendment 2026-11-23: the web UI in the production compose file (PROPOSED)

Owner decisions #14a–#18a, #42a, #43a and #47a–#56b. The web UI becomes the compose service `web`.
Nothing under `web/` changes for it (#15b). It is defined, not deployed: the owner deploys it.

- **Image (`docker/Dockerfile.web`, #17a+b, #42a).** Two stages on `node:24-bookworm-slim`. The
  build stage runs `npm ci --ignore-scripts`, the normal `next build` and both build checks
  (`check-no-spectrum.mjs`, `check-built-css.mjs`). The runtime stage installs with `npm ci
  --omit=dev --omit=optional --ignore-scripts`, fails if `sharp` or `@img` is present, copies only
  this platform's SWC binary, the `.next` output, `next.config.ts` and the launcher, and runs as
  `node` (uid 1000). The build context is cut down by `docker/Dockerfile.web.dockerignore`.
- **Secrets (#47a, #50a, #53a).** Two compose file secrets on `web` only:
  `divesafe_web_session_secret` (64 lower-case hex characters) and `divesafe_web_actions_key` (32
  bytes, base64). `docker/make-secrets.sh` creates both and never overwrites one. The launcher
  `docker/web-start.cjs` refuses to start if either is set in the environment, missing or in the
  wrong form, sets them for its own process, opens the API relay and then starts `next start` in
  that process. No `env_file`. `make-secrets.sh` checks both files' forms after writing them.
- **Path to the API (#48a, #52a, #55a).** `web` publishes `127.0.0.1:3100` only (#18a). Its own
  code calls `http://127.0.0.1:8000`. The launcher listens there, in the same Node process as Next,
  and forwards each connection to the API's fixed address `10.231.78.10` on the internal network
  `web-api` (`10.231.78.0/24`, pool `.128/25`, so no other container gets that address). It never
  looks a name up. Idle connections close after 120 s on either side, at most 64 are open at once,
  the process exits if it cannot listen (#54a), and Next starts only once it listens. Because the
  relay is part of the process, a restart of `web` restarts it too. The web code's own API timeout
  (`API_TIMEOUT_MS`, 30 s) is shorter than the relay's idle timeout, so the relay never cuts a call
  the web code is still waiting for; a reset connection is shown as an unknown outcome to check
  before retrying, like any other unanswered call (#55a replaces the separate
  `web-relay` service of #48a, which lost its network after a `web` restart). (The "API timeout
  (120 s)" that the login-throttle Known gap above once held is corrected to 30 s by the pre-deploy
  fixes amendment below.)
- **Hardening (#54a).** `init: true` and `pull_policy: build` on `web`; a read-only root file
  system, a small tmpfs, memory and process limits; a healthcheck that calls only loopback and
  passes on 200 alone. `DIVESAFE_WEB_TRUST_PROXY` is fixed at `"0"` (`"1"`, and no published port,
  from the Caddy amendment 2026-11-24). The healthcheck covers Next
  only, not the relay. Uptime Kuma watches the API's `/health` directly (ADR 0027 item 3), not
  through the relay; a broken relay path shows as errors in the web UI, not as an alert.
- **Deploying and rolling back.** Update the production checkout from a shell with `umask 022`
  (#72b). Before each `--build`, keep the running image: `docker tag
  divesafe-web:local divesafe-web:rollback-<date>`. Always name the services: a bare `C up -d`
  starts `web` too. The first time, `C up -d --no-build --wait api` once, so the API joins `web-api`
  at its fixed address, then `C up -d --build --wait web`. Roll back by tagging the old image as
  `divesafe-web:local` and `C up -d --no-build --wait web` (checked in a scratch run: with
  `--no-build`, `pull_policy: build` does not rebuild; a later `up` without it does).
- **Tests and CI.** `tests/safety/test_production_compose.py` pins the services, networks,
  secrets, every instruction of `Dockerfile.web`, the dockerignore, the launcher (relay included)
  line by line and `make-secrets.sh`'s web lines (#51a: strengthen only). The CI job `web-image`
  builds the image; checks it runs as `node` (uid 1000) with no `sharp`, `@img` or `.env` file;
  checks that the built UI carries the degraded-evidence note; checks that the launcher refuses
  either secret set in the environment, a missing secret file and a malformed one, each with its
  fixed message; and starts it with throwaway secret files, no network, no capabilities, an init and
  compose's limits until `/login` answers 200. It then checks that only one process runs besides the
  init, that no process environment (Next's included) holds a secret name, and that Next still
  prefers the runtime server-actions key. A last step starts it on an internal network with a stub
  API at `10.231.78.10` and checks that the relay reaches it. `trivy.yml` scans the web image too;
  its SARIF categories are now `trivy-api` and `trivy-web`.
- **Deploy precondition (ADR 0008 Amendment 1, Open preconditions (2026-11-22), control 2).**
  Before `web` is deployed, and before `DIVESAFE_DEGRADED_MAY_SUPPORT_GO` is set to `true` while
  `web` runs: (a) the image being deployed must include `DegradedEvidenceNote`. CI job `web-image`
  checks this on every build, so deploy only an image built from a commit where that job passed.
  The checks on `fbad5a2` and `30ebce7` were not deployed builds. Before `up`, run the same check
  on the image that will run: `docker run --rm --network none --entrypoint sh divesafe-web:local -c
  'grep -rqF --include="*.js" "based on model and forecast data, not observations"
  /web/.next/server'` (exit 0 required). (b) Every client a decider uses
  must be checked by hand to show `degraded_evidence_note`. Nothing enforces (b).
- **Web has a route out.** `web-ingress` cannot be internal, because Docker cannot publish a port
  from an internal network (#49a). Only the API path is fixed; a compromised Next process could
  reach the internet. The relay is not a boundary: a compromised Next process can reach the API
  directly at `10.231.78.10:8000` on `web-api`. (Caddy amendment 2026-11-24, built in code, not
  deployed: web leaves `web-ingress`, so this route out moves to caddy.)
- **The secrets are readable inside the web container.** The secret files stay mounted under
  `/run/secrets` for the container's lifetime (mode 0444), and the launcher puts both values in the
  `process.env` of the one Node process (Next and the relay). They are not in
  `/proc/<pid>/environ`, because they are set after the process starts. Anything that runs code in
  the container can read them: from the files, from the process, or from its memory (same uid,
  subject to the host's Yama setting). A process Next starts with default options would inherit
  them. The launcher starts none, and a test bans it from doing so. CI checks that Next has started
  none after start-up and three page loads (job `web-image`). Other code paths are not checked.
  Checked for Next 16.4.6; re-checked on every Next upgrade (CI tripwire, job `web-image`).
- **The build bakes a random server-actions key** into the manifest. It is unused while the
  runtime key is set (above), and the launcher refuses to start without one.
- **Image supply chain.** The base image is pinned by tag, not digest. Trivy reports; it does not
  block a merge.
- **Plain HTTP and one shared bucket** (fix built in code, not deployed: amendment 2026-11-24).
  Until a TLS proxy is in front and
  `DIVESAFE_WEB_TRUST_PROXY` and `allowedOrigins` are set, the web UI is for loopback use only, the
  session cookie is `Secure` (so a browser over plain HTTP on another host drops it), and every
  sign-in shares the web server's throttle bucket at the API.
- **Next listens on 0.0.0.0 inside its container**; only the compose `ports` line keeps it on the
  host's loopback. Docker Engine before 28 did not fully block other hosts on the LAN from a
  loopback-published port; the host runs 29.8.2. It listens on `web-api` as well, so `api` can
  also reach it there.
- **The relay is plain TCP**, so it carries the bearer key in clear text on `web-api`, a network
  only `api` and `web` join. No test can see a host route; on a new host check that nothing
  (LAN, VPN) overlaps `10.231.78.0/24` with `netstat -rn` (on the Linux VM, `ip route`, and
  `10.231.79.0/24` to `10.231.81.0/24` too: amendment 2026-11-24).
- **npm, yarn and corepack are in the runtime image** (#56b). They come with the
  `node:24-bookworm-slim` base image and Next does not need them at runtime. Code that runs in the
  container could use them to fetch and install packages through web's route out. Removing them is
  a later Dockerfile change.
- **The Spectrum packages are on disk in the runtime image** (#43a). They are runtime
  dependencies of `web/package.json`, so `npm ci --omit=dev` installs them under `node_modules`;
  the build check keeps them out of what the browser loads. Dropping them is a later dependency
  change.
- **DNS rebinding to `127.0.0.1:3100`.** Next does not check `Host`. A page in the operator's
  browser can rebind its own name to the loopback port and use the pages that need no session,
  including the `login` action. It never gets the session cookie (that cookie belongs to
  `127.0.0.1`), but with `DIVESAFE_WEB_TRUST_PROXY=0` its failed guesses fill the one shared
  throttle bucket and lock everyone out of sign-in for a minute at a time. The API port
  `127.0.0.1:8000` has the same exposure, behind bearer authentication. Closed for the web UI by the
  pre-deploy fixes amendment below (`web/proxy.ts`, #70b); the API port is unchanged.
- **The `Secure` cookie attribute is not yet checked with a real sign-in** in the container
  (`NODE_ENV=production` is set in the image, which is what turns it on).

## Amendment 2026-11-23: pre-deploy fixes (owner #69a, #70b, #71a)

One reviewed change to `web/` before the first deploy, from the risk and security reviews of the
web code. #15b (`web/` unchanged by the deploy) is set aside for this change only (#69a).

- **Host allow-list (#70b).** `web/proxy.ts` runs before every request and answers 421 unless
  `Host` is `127.0.0.1:3100` or `localhost:3100` and any `X-Forwarded-Host` is one of them too.
  The two names are written in `web/lib/hosts.ts`; there is no setting. The TLS proxy change
  (#67a, #68a) replaces them with `DIVESAFE_WEB_ALLOWED_HOSTS` (built in code: Caddy amendment
  2026-11-24; `localhost:3100` is dropped).
- **Start-up checks.** `web/instrumentation.ts` calls `web/lib/startup.ts`, which reads the session
  secret and the API address once at start. Next 16 only logs an error thrown from `register()` and
  goes on serving (checked: every page answered 500), so the check exits the process itself with
  fixed text.
- **A page that fails to render** shows fixed text (`web/app/error.tsx`, and
  `web/app/global-error.tsx` for the root layout): "NO RESULT. This page failed to load. Do not act
  on it." No error detail reaches the page.
- **Assessment page.** The pending, decision-withheld and decision-recorded lines come before the
  system's badge, and a pending badge is labelled "not yet decided by a human". The decided line
  is worded from the two outcomes and calls out an override to a less severe outcome; a reply whose
  `is_override` or `overrides_to_less_severe` disagrees with the outcomes is refused like any other
  inconsistent reply. The proposal note is worded from the outcomes, not from `llm_tightened`.
  A GO or CAUTION whose response lacks `degraded_evidence_note` shows a fixed note to treat it as
  resting on model or forecast data. The site-document note (`knowledge_note`, ADR 0022 decision 14)
  is shown; when the field is missing the page says site hazards may not have been checked.
  Conflicting evidence set aside before the rules ran is counted in a warning card and in the
  decision form's context. A finding that cites no evidence is marked. The decision form comes
  after every problem section, and its choices run NO-GO first. Times show UTC and UTC+8.
- **Typed text.** The override reason, the decider's name and the conditions notes refuse control,
  format and text-direction characters, line and paragraph separators, private-use and unassigned
  code points, characters Unicode marks as drawn as nothing (the Hangul fillers among them) and the
  blank braille pattern. The decider's name also refuses line breaks and tabs. Browser line breaks
  are normalised before the check and before the 409 comparison. A reason without a visible
  letter, digit, punctuation mark or symbol is refused by the form, and a stored one (from another
  client) is shown as "no visible text: treat as no reason given". Site ids are checked against the
  API's pattern. The check is strict on purpose: an emoji with a variation selector (such as a
  warning sign) or a character newer than the server's Unicode data is refused with the normal
  message.
- **Other.** Every server action refuses a call that is not a form; only this app's own pages, or
  an address typed into the browser, can sign the person out (a link from another site, or from
  another service on this machine, cannot; a browser that sends no `Sec-Fetch-Site` is let
  through; a refused sign-out answers with fixed plain text, not a redirect, so it cannot loop);
  `Retry-After` is shown only as a whole number of seconds up to a day; provider attribution is
  labelled as supplied by the provider and not checked.

### Checked on the built app (2026-11-23)

`next start` from the production build, on `127.0.0.1:3100` with a throwaway secret and no API:
no session secret, or an API address that is not loopback or https, exits with status 1 and the
fixed line; the right host answers 200 by both names; a wrong `Host` or `X-Forwarded-Host` answers
421 for a page, a static file and a server-action POST; logout ends the session for same-origin,
`none` and no header, and not for `same-site` or `cross-site` (fixed text, no redirect); a
sign-in form posted as a browser would reaches the API call (the `FormData` guard passes), and the
throwaway secret is not in the log. `next build` with no session secret in the environment
completes, so the start-up check does not run during the build. The same checks run on the image
at deploy (deploy note).

### Known gaps added

- **Use DiveSafe in its own browser profile (#71a).** Over plain HTTP on loopback, the session
  cookie is not `__Host-` prefixed and other pages in the same profile share the browser's state.
  Sign in only from a browser profile used for nothing else. The `__Host-` prefix comes with TLS
  (#68a).
- **A 409 match ignores who decided.** After a 409 the decision form goes to the record when the
  stored decision and reason equal the ones submitted. It does not compare `decided_by`, so an
  identical decision recorded by someone else in the meantime reads as this person's.
- **An invisible reason is refused by the web UI only.** The API still accepts an override reason
  made only of invisible characters from another client; the page shows it as no reason given. No
  API change is made now (owner). The web form refuses a non-empty reason without a visible
  letter, digit, punctuation mark or symbol in any script (the same rule the page uses to show it).
- **Joiners are refused in typed text.** U+200C and U+200D (zero-width non-joiner and joiner) are
  format characters, so Persian text, some Indic spellings and joined emoji cannot be typed.
- **Cross-examination summaries are not shown.** Only their conflicting signals reach the page;
  this predates the change.
- **Real browsers.** Sign-in is checked by hand in Chrome or Firefox at deploy; Safari may refuse
  the `Secure` cookie on plain HTTP.

## Amendment 2026-11-23: file modes in the web image (owner #72b, #73a)

The first deploy build failed to start: Node could not read `/web/web-start.cjs`. Git records
only whether a file is executable, so a checkout made under a strict umask (077) holds `0600`
files; `COPY` kept those modes with root as owner, and the image runs as `node`. The CI checkout
uses normal modes, so CI did not catch it.

- **Fix.** Every `COPY` in `docker/Dockerfile.web` that takes files from the checkout sets
  `--chmod=u=rwX,go=rX` (files 644, folders 755, executables keep their bit). The three
  `COPY --from=build` steps are unchanged: `.next` and the SWC binary are written in the build
  stage with normal modes, and `next.config.ts` comes from the checkout, so its mode is set by the
  build stage's `COPY --chmod`.
- **Tests.** `tests/safety/test_production_compose.py` pins the mode on every checkout `COPY`, as
  well as the whole Dockerfile. Dropping any `--chmod` fails at least two tests (three for the
  launcher's). The CI `web docker image` job makes `web/` and the launcher owner-only before it
  builds, then checks as `node` that everything under `/web` is readable (folders enterable) and
  that nothing there is world-writable, setuid, setgid or owned by anyone but root; a folder
  `node` cannot enter, or any `find` error, fails the step. `next build` writes its output
  group-writable for root's group, so `node` must not be in group 0: the image's groups are
  checked there, and the compose `web` service may set neither `user` nor `group_add`.
- **The API image is unchanged (#73a).** The app runs from site-packages, which a non-editable
  `pip install .` writes with normal modes, and `/app/src` is never imported. The install line is
  pinned in the same test file, which also refuses `PYTHONPATH`, `--app-dir` and `/app/src`
  outside a `COPY`, in the Dockerfile and on the services built from it. An editable install, or
  running from `/app/src`, would bring the problem back.
- **Checked.** A web image built from a umask-077 clone with this change reads its launcher as
  `node` and stops with the fixed message when the secret files are missing; with the `--chmod`
  dropped from the launcher's `COPY`, or from `COPY web/ ./`, the CI check fails. An API image
  built from the same clone imports the app as its unprivileged user.
- **Build requirement.** Symbolic `--chmod` needs a recent BuildKit Dockerfile frontend (the
  production host has BuildKit 0.33); an older builder, or the legacy builder, refuses the build.
  No `# syntax=` line is added, so no frontend image is pulled at build time.
- **Not covered.** Files bind-mounted from the checkout (the database's `pg_hba.conf` and init
  script) keep their host modes: a checkout updated under umask 077 could make them unreadable
  inside the database container, which stops it from starting. "Deploying and rolling back" above
  and the header of `docker/docker-compose.production.yml` say to update the production checkout
  from a shell with umask 022. Secret files are not in the checkout; they keep the mode they were
  created with, so create them readable by the service that reads them (`docker/make-secrets.sh`
  writes them 0444 in a 0700 folder).
- **Known gap: the API Dockerfile is not pinned line by line.** The test refuses an editable
  install spelled with `pip`, `-e` or `--editable`, but another installer that installs editable
  by default (for example `RUN uv sync`) would pass, and a `RUN set -e` line would be refused by
  mistake. Neither line exists today. Closing it means pinning the whole API Dockerfile as
  `Dockerfile.web` is pinned, in a later reviewed change.

## Amendment 2026-11-24: Caddy in front of the web UI (owner #67a, #68a, #70b, #86b, #91b–#114a)

ACCEPTED (owner, 2026-11-28, #144a). Designed 2026-11-24; built in code by the Caddy PR
(2026-11-28: `docker/Caddyfile`, the `caddy` service and networks in
`docker/docker-compose.production.yml`, and the `web/` changes below), not deployed anywhere
(ADR 0024 Amendment 2, the move to the Linux VM). It carries out the TLS proxy change that #18a,
#67a and #68a left for later. Review items R1–R9 (security review of `web/` before remote
access, 2026-11-22) are mapped below.

- **Caddy is the single trusted proxy (#91b).** A `caddy` service in the production compose file,
  `caddy:2.11.9-alpine` pinned by digest, in front of `web` only: user `65534:65534`, read-only
  root file system, `cap_drop: [ALL]`, `no-new-privileges`, memory and process limits, `admin off`
  and `persist_config off` (no admin endpoint at all). It forwards plain http to
  `10.231.79.20:3100`, web's fixed address on the internal network `web-proxy`
  (`10.231.79.0/24`, pool `.128/25`; Caddy `.10`, web `.20`). Caddy dials the address, never the
  name `web`, because Caddy has a route out (below). Started on its own after web:
  `C up -d --wait caddy`.
- **Two sites, no catch-all (#106a, security S9).** Caddy publishes one port,
  `<vm-address>:443` to its `8443`, on the VM's LAN address only (`DIVESAFE_LAN_ADDRESS`; never
  `0.0.0.0`), with no `DOCKER-USER` rule (#92c). The LAN site, `https://<name>.home.arpa`, uses
  `tls internal` (#86b) and listens on `10.231.81.10:8443` only. The public site,
  `http://<public-name>`, listens on `10.231.80.10:8080` only, Caddy's fixed address on
  `pangolin-edge`, the internal network it shares with Newt alone (#100a). Both names come from
  `DIVESAFE_WEB_LAN_NAME` and `DIVESAFE_WEB_PUBLIC_NAME` in `production.env`, which compose
  requires, so neither site can become a catch-all through an empty name. A TLS client that asks
  the LAN listener for any other name, or for none, gets no certificate, so TLS fails. The LAN
  server sets `strict_sni_host on`: the `Host` must equal the TLS name, else Caddy answers
  `421 Misdirected Request` and proxies nothing. So the right TLS name with a foreign `Host` gets
  421, and a handshake with no TLS name, if one ever succeeded, would still get 421 and never
  reach web; that listener needs no refusal site. (Found by the live test, where a foreign `Host`
  got Caddy's empty response, and fixed in this PR.) The public listener has an abort-only site
  (`http://:8080`): a request whose `Host` is another name, an address, or missing has its
  connection closed with no response, and is never proxied. HTTP/3 is off (`protocols h1 h2`;
  only TCP 443 is published), and there is no http-to-https redirect listener.
- **Networks (#49a, #52a, #100a).** Every address below is fixed and outside its network's
  `ip_range`:

  | Network | Subnet | Internal | Members |
  | --- | --- | --- | --- |
  | `web-ingress` | `10.231.81.0/24` | no | caddy `.10` (carries the published port) |
  | `pangolin-edge` | `10.231.80.0/24` | yes | caddy `.10`; Newt `.20`, joined from outside |
  | `web-proxy` | `10.231.79.0/24` | yes | caddy `.10`, web `.20` |
  | `web-api` | `10.231.78.0/24` | yes | api `.10`, web `.20` (web's address newly fixed) |

  `pangolin-edge` has the fixed name `pangolin-edge` and is created by the `divesafe-prod`
  project; Newt (its compose file is outside this repository) joins it as `external: true`, so
  caddy is started before Newt, or the network does not exist yet. `web-ingress` is caddy's only:
  Docker cannot publish from an internal network, so caddy, not web, now has the route out
  (#49a). Its subnet was Docker's choice (`172.x`) before this change, and Docker does not change
  an existing network's subnet in place: on a host that has the old one, remove it (nothing
  attached) and let compose recreate it. The VM is a fresh project. Each new subnet is checked
  against the host's routes with `ip route` on the VM.
- **web's only way in is Caddy.** `web` has no published port and joins `web-proxy` and `web-api`
  only, both internal, so it has no route out any more (#49a's gap closes for web). Without this,
  any local process could reach web directly and send its own `X-Forwarded-For`, which
  `TRUST_PROXY=1` would believe; Next cannot check which peer sent a request. `api` can still
  reach web on `web-api` (Known gap above); the API is trusted code.
- **Forwarded headers (#98a, security S2, R1, R5).** The LAN server trusts no proxy, so a device's
  own `X-Forwarded-For` is never believed and the peer is the device. The public server trusts
  only `10.231.80.20/32` (Newt) with `trusted_proxies_strict`, so the header is read right to left
  and the first untrusted address from the right, the one Pangolin appended, is the client. Both
  sites then replace the header: `header_up X-Forwarded-For {client_ip}` (exactly one address, so
  web's last-entry rule in `web/lib/throttle.ts` cannot pick Newt's, R1) and
  `header_up X-Forwarded-Host {host}` (the site's own name, never one the client sent, R5). `Host`
  passes through unchanged. **Two checks** (security R3-S2): (a)
  `tests/integration/test_caddy_forwarding_live.py` runs the real Caddyfile in the pinned image,
  sends a forged `X-Forwarded-For` from the simulated Newt address and from another peer, and
  shows web receives exactly one address: the device's on the LAN site, or on the public site the
  one Pangolin reports. It runs in CI with the caddy and node images pulled by digest, and CI
  fails if it skips (#149b); `tests/safety/test_caddyfile.py` pins the Caddyfile offline; (b) CI
  cannot reach Pangolin, so the live check in ADR 0024 Amendment 2, B1 item 3, shows that a
  forged header sent through Pangolin lands in the sender's real bucket.
- **Trusted proxies at web and the API.** `DIVESAFE_WEB_TRUST_PROXY=1` is set in the same change
  that makes Caddy web's only way in. The API's `DIVESAFE_TRUSTED_PROXIES` is fixed in the compose
  file to `10.231.78.20`, web's address on `web-api`, and is no longer read from `production.env`
  (removed from the example), so it cannot be widened there: never a subnet, a gateway, or an
  address on `divesafe-trace` or `divesafe-health`. **Every API call carries the address (R2):**
  `web/lib/client.ts` reads `X-Forwarded-For` when, and only when, the proxy is trusted, and
  `web/lib/api.ts` sends it with every call, not only at sign-in, so a replayed revoked-key cookie
  fills its sender's bucket at the API, not everyone's.
- **One host allow-list (#70b).** `DIVESAFE_WEB_ALLOWED_HOSTS` replaces the two names written in
  `web/lib/hosts.ts`. Compose builds it from `DIVESAFE_WEB_LAN_NAME`, `DIVESAFE_WEB_PUBLIC_NAME`
  and `127.0.0.1:3100` (web's own healthcheck, which calls loopback inside the container); it is
  never set in `production.env`. `localhost:3100` is gone. Web refuses to start if the list is
  missing, empty, malformed or holds a wildcard. `127.0.0.1:3100` is accepted only with no
  `X-Forwarded-Host`; through Caddy both `Host` and `X-Forwarded-Host` must be one of the two site
  names (R5).
- **Server-action origin check (R6, #141a).** `serverActions.allowedOrigins` stays unset. Next
  compares a server action's `Origin` with `X-Forwarded-Host`, or `Host` without one, and Caddy
  fixes `X-Forwarded-Host` to the site's own name, so the comparison is with a name Caddy chose.
  This replaces the design's plan to read `allowedOrigins` from the host setting.
  `web/tests/origin.e2e.ts` (`npm run test:e2e`, run in CI against the built server after the
  build) replays the real sign-in form. A foreign `Origin` (another site, the LAN name on
  `http:8443`, a look-alike suffix, the loopback name, `null`) is refused (500) and nothing
  reaches the API. The site's own `Origin` reaches the API with the client's address and gets
  exactly the `__Host-` cookie with the attributes below. A foreign `Host` or `X-Forwarded-Host`
  gets 421 before any page. Setting `allowedOrigins` to the foreign origin makes the test fail
  (mutation-checked). A POST with no `Origin` is let through: that is Next's behaviour, and such a
  hand-made request holds only what its sender already had (Known gap above).
- **TLS and the local CA (#86b, #100a).** Caddy's local CA issues the LAN name's certificate. Its
  root is in the `caddy-data` volume (`caddy/pki/authorities/local/root.crt`); the owner installs
  it on each device that uses the LAN name (ADR 0024 Amendment 2, Access model). The leaf lasts
  about 12 hours and Caddy renews it. With `skip_install_trust`, Caddy never tries to install the
  root itself. The public name's certificate is held by the Pangolin server, which ends TLS (#94b,
  #95a); Newt to Caddy is plain http on `pangolin-edge`.
- **Cookie (#68a, #71a, R3, #139a).** The session cookie is `__Host-divesafe_session`, always
  `Secure` (no longer tied to `NODE_ENV`), `HttpOnly`, `SameSite=Strict`, `Path=/`, no `Domain`,
  and no `Max-Age` or `Expires`, so it ends when the browser closes; the sealed expiry still ends
  it after 8 hours (`web/tests/cookie.test.ts`; on the built server, `origin.e2e.ts`). Signing out
  removes the browser's copy only. To end a session that may have
  been copied, revoke the API key or rotate the session secret (which ends every session).
- **HSTS.** Web sends `Strict-Transport-Security: max-age=31536000` itself (`web/next.config.ts`),
  not Caddy, so both paths carry exactly one such header (the Pangolin path's TLS is not Caddy's).
  No `includeSubDomains`, no preload (#145a).
- **Access log (R4, #140b).** One JSON access log per site, `/data/caddy/logs/access-lan.log` and
  `access-public.log` in the `caddy-data` volume, rolled at midnight and kept 14 days
  (`roll_keep_for 336h`). Caddy logs `Cookie`, `Set-Cookie`, `Authorization` and
  `Proxy-Authorization` as `REDACTED`; `log_credentials` is never set. The log still holds client
  addresses, paths, times and the `Host`. That old files are removed after 14 days is not yet
  observed (host checklist).
- **Limits (R7, #142a).** `request_body` at most 1 MB per site (Next's server-action default);
  server timeouts `read_header 10s`, `read_body 30s`, `write 60s`, `idle 2m`. A 60 s write is
  enough: web's API calls abort at 30 s, `RunProgress` polls, and nothing streams. A body whose
  `Content-Length` is over 1 MB is refused before anything reaches web; a chunked over-size body
  streams up to 1 MB to web before the 413 cut, and Next's own 1 MB server-action limit applies
  on top. Host checklist:
  two LAN devices sign in wrongly and land in different throttle buckets (a userland proxy could
  show them all as one gateway address).
- **Overrider keys over the LAN name only (#108a, #114a).** Owner discipline, not enforced by the
  web UI; any overrider key typed on the public name is rotated. A later option: web refuses an
  overrider session when the `Host` is the public name (it would need the session's roles).
- **L1–L4 are lost (#138a).** The owner's instruction named L1–L4 for this PR, but the list was
  lost, and no item with those labels is defined in this repository. A new security review of
  `web/` produced R1–R9 instead; they are new items, not L1–L4 restated. Where each is handled:

  | Item | Handled |
  | --- | --- |
  | R1 one client address | `{client_ip}`, Newt-only strict trust; `test_caddyfile.py`, |
  | | `test_caddy_forwarding_live.py` |
  | R2 address on every API call | `web/lib/client.ts`, `web/lib/api.ts`; `login.test.ts` |
  | R3 cookie | `__Host-` cookie, revocation wording (#139a); `cookie.test.ts`, `origin.e2e.ts` |
  | R4 access log | Access log above (#140b); `test_caddyfile.py` |
  | R5 forwarded host | `{host}`, loopback name only without it; `hosts.test.ts`, |
  | | `test_caddy_forwarding_live.py`, `origin.e2e.ts` (421) |
  | R6 action origin | `allowedOrigins` unset (#141a); `origin.e2e.ts`, mutation-checked |
  | R7 limits | body and timeout limits (#142a); `test_caddyfile.py`; two-device checklist item |
  | R8 nonce CSP, per-session revocation | later: Known gaps below |
  | R9 relay fairness | later: Known gaps below |

- **Not a Pangolin target:** only Caddy's public-name site is. The API port, Uptime Kuma, Langfuse
  and SSH are not (#85b, #96a).

### Known gaps changed by this amendment

- **Plain HTTP and one shared bucket** (above) stays true until the Caddy PR is deployed. After
  it, the LAN path has per-device buckets (to observe: the two-device item), and the Pangolin path
  has per-client buckets as far as the address Pangolin appends is right (#98a; ADR 0024
  Amendment 2, Known gaps), at web and at the API alike (R2).
- **Web has a route out** (above) moves to caddy: web's two networks are internal, and
  `web-ingress`, which is not, is caddy's only. A compromised Caddy could reach the internet and,
  on `web-proxy`, web. npm, yarn and corepack (above) then have no route out from web.
- **Use DiveSafe in its own browser profile** and **`Secure` not checked with a real sign-in**
  (above): the cookie is now `__Host-` and always `Secure`; a real sign-in through Caddy in Chrome,
  Firefox and Safari is still to be checked at the synthetic first run.
- **DNS rebinding to `127.0.0.1:3100`** goes away with the published port. The host allow-list
  still refuses any other `Host`. The API port `127.0.0.1:8000` keeps its exposure.
- **Next listens on 0.0.0.0 inside its container** stays, on `web-proxy` and `web-api` only, with
  no published port. The VM runs Docker Engine 29.8.2 or later (#97a, #121a); re-check after any
  host change.
- **Check routes with `ip route`.** The relay gap above says `netstat -rn`; on the Alpine VM check
  `10.231.78.0/24` to `10.231.81.0/24` with `ip route` (BusyBox may not provide `netstat`).
- **New: web believes `X-Forwarded-For` on `web-api` too (accepted).** Web listens on all its
  interfaces (`next start -H 0.0.0.0`, `docker/web-start.cjs`), so code in the API container
  could call web directly with a forged `X-Forwarded-For`, which web believes because it trusts
  the proxy. Accepted because the API is trusted code. Binding web to its `web-proxy` address
  alone would break its loopback healthcheck and the CI container run (network `none`), so that
  is left for later.
- **New: the site names reach the Caddyfile as raw text.** Caddy inserts `DIVESAFE_WEB_LAN_NAME`
  and `DIVESAFE_WEB_PUBLIC_NAME` unchecked; only web checks them as host names, so an operator
  typo could change the parsed config. The host checklist compares both with web's allowed-host
  list before the first start.
- **New, if the host check finds it: a VM shell can reach web directly.** On a rootful Linux
  Docker host, host processes may reach web's `web-proxy` address on port 3100 despite the
  internal network; ADR 0024 Amendment 2's host checklist records whether they do. If so, a
  local user with a shell on the VM could forge `X-Forwarded-For` at web and rotate throttle
  buckets at web and the API. Keys are random and at least 128 bits, and a shell on the VM is
  already needed. A host firewall rule is a later owner decision; the assistant never changes the
  VM's firewall.
- **New, later (R8):** the CSP still allows inline scripts (a per-request nonce in `proxy.ts` is
  not built), and there is no per-session revocation (a session id or a revocation counter in
  the sealed payload); revocation is per key, or for every session at once.
- **New, later (R9):** the relay's 64 connections (`docker/web-start.cjs`) are shared by every
  client with no per-client limit, so one client can hold them all. The later fix is a
  per-client limit in web, with no Caddy plugin and no new dependency (#147a).
- **New (#146a):** the access log holds client addresses and paths for 14 days in `caddy-data`,
  the volume that also holds the local CA's key; it is outside DiveSafe's backups. Kept so for
  now, to revisit with the backup PR: a separate log volume made Caddy exit at start-up.
- **New:** every device on the home network, and anyone past Pangolin authentication (#110a),
  reaches the DiveSafe sign-in page (#92c, #94b). What stands in front of an API key there, in
  order, is in ADR 0024 Amendment 2 (Access model): Pangolin authentication on the public path,
  the key's secrecy and randomness, then the per-source failure limits.
- **New:** the Pangolin server sees every page, the API key typed at sign-in and the session cookie
  in clear (ADR 0024 Amendment 2, Known gaps). Signing out of DiveSafe does not end the Pangolin
  session.
