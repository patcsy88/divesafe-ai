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
- Evidence items are labelled by `data_kind`: only an observation is "measured". Notices and
  knowledge are "text, not a measurement". Quality tags are not sufficiency, and the page says so.
- **Withheld evidence box.** When the evidence response carries `withheld_evidence` (items dropped
  because two sources disagreed under one id, ADR 0008), the evidence page shows a separate,
  labelled box above the evidence list: "Withheld evidence - NOT used, NOT evaluated, may hold a
  more severe result", the server's `withheld_note`, then each item. Everything is plain text: no
  link, no anchor, no DOM id from an item, no raw HTML; ids are shown as text; raw values sit in a
  collapsed block through the same `shown()` helper (cut at 20,000 characters). At most 50 items are
  shown, ordered by id, followed by "N more withheld items not shown". Source, source version,
  category and each quality note are cut at 1,000 characters (the normal evidence list is not).
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
  recorded). A late release can remove another attempt's hit when the API timeout (120 s) exceeds
  the 60 s window. `login` is a `"use server"` action with no direct test; the library is tested.
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
  request without an `Origin` header through.
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
refuses `aria-hidden`, `inert`, `hidden` and `popover` in the app's own source, in any letter case:
as JSX attributes, `createElement` props keys (shorthand, quoted and computed ones included),
property assignments (dotted, by name, or through `Object.assign`, `Reflect.set` or
`defineProperty`), or as quoted strings anywhere (a form field's `type="hidden"` aside), so a name
held in a variable is refused too. `\u` escapes and `\x` letter escapes are refused, and every write
through a computed name (`obj[key] =`) is pinned by file. Every props argument is `null` or an
object literal written where the element is built, so props made elsewhere never reach an element;
spreads into props, `cloneElement` and the JSX runtime are refused, and a capitalised tag must be a
component the file imports or declares. It also refuses `setAttribute`, `removeAttribute`, HTML
writes, `eval` and code built from strings, `<dialog>`, `<template>`, `<noscript>`, `<datalist>`,
`<svg>`, inline styles, `role` none or presentation, `classList` and `className` writes, imports
from `tests/`, and removing text (`textContent`, `innerText`, `outerText`, `nodeValue`, `.data`,
`replaceChildren`, `replaceWith`, `removeChild`, `.remove()`, `.open =`); it pins every `aria-label`
and `aria-labelledby` and every `<details>`, by file, and refuses a banner, badge, notice, role or
the DEGRADED note inside one. `lib/` builds no elements and does not look up the page (`document`,
`window`, `querySelector`); it can still change an element it is given, which the write checks
cover. Every script file under `web/` outside `app/`, `components/`, `lib/` and `tests/` is on a
reviewed list, and none of those folders may be a link. Classes are `classes.test.ts`'s: only `ds-`
classes styled in `globals.css`, so no Tailwind hiding utility. It reads the source as text, not as
a parsed program, so it is a tripwire, not proof: a name built at run time is not seen, nor is what
a HeroUI or React Aria component adds. A failure on text that is not an attribute is fixed by
rewording the text, not by loosening a pattern. Not checked (Known gaps, owner, 2026-11-23):
`aria-live`, `aria-busy` and other live-region attributes; hiding declarations in `globals.css`
itself (`classes.test.ts` checks class names, not what a `ds-` rule does); and other safety text
inside the two existing `<details>` than the names refused there (the (a) harness opens every
`<details>` before measuring, so it would not see this either). Adding the test changed `web/`, so
it ended the `web-baseline-20261123` deploy baseline; the baseline moved to
`web-baseline-20261123-2` on the build comparison recorded in ADR 0008 Amendment 1 (a). The
checklist is never narrowed to skip `web/tests/`.

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
