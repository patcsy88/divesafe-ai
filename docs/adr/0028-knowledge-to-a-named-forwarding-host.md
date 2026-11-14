# ADR 0028: Approved knowledge may go to one named forwarding model host

Status: PROPOSED (2026-11-14; owner answers recorded below; four review rounds, the last with
no blockers and no should-fix from `security-reviewer` or `risk-reviewer`). Relaxes ADR 0022
decision 15 (knowledge reaches a prompt only under `declared_local`), so it needs the owner's acceptance and both reviewers (config,
the egress gate, the knowledge store, `domain`, `agents`, `tests/safety/`). Nothing is built.
Amends ADR 0022 (decision 15) and ADR 0023 (the open "trusted host" decision); does not amend
ADR 0021.

## Context

- Production runs `claude-sonnet-5-5` through a routing proxy (CLIProxyAPI) at
  `https://wall-e.ch30ng.org/v1`, on a public address, forwarding to Anthropic. Basis
  `remote_external_opt_in`, so every `kb:` item is withheld: `AssessmentRecord._check_egress`
  (`domain/models.py`) refuses an unwithheld `kb:` item unless `declared_local`;
  `split_for_egress` (`agents/stage.py`, `_declared_local`) sends knowledge only under that exact
  string; `check_egress` (`models/egress.py`) gives the basis.
- **Owner trial (2026-11-13/14).** Throwaway database, uncommitted trial code on another branch
  (never merged), trial-only basis `trial_forwarding_opt_in`. Claude through the same proxy, reached
  by its LAN address over http (a trial-only exception, not proposed here), three plan pairs back to
  back (guide withheld vs sent) plus one earlier run. With the guide sent, the site agent cited
  `kb:tioman-site-guide:2:1` and listed its hazards (currents at Chebeh and Tiger Reef, overhead and
  entanglement at Soyak and Labas, boat entry and operator checks, traffic at Renggis). It stayed
  inside the guide: said it is static and not live, said the plan names only the island so hazards
  cannot be matched to a dive site, never used it to make a dive look safer. All outcomes
  INSUFFICIENT EVIDENCE, Supervise 5/5, 0 downgrade attempts. 1 of 54 calls failed (a weather
  specialist hit the 3000-token limit; unrelated to knowledge). A local `gpt-oss-20b` on loopback
  (`declared_local`) also used the guide but took 5-10 min and failed 1-4 of 9 calls. This is the
  owner's report of a few runs, not an evaluation; the trial records are not in the repository.
- ADR 0023 left open whether a "trusted host" declaration should exist for a non-loopback server.

## Decision

1. **Transport unchanged.** `https://wall-e.ch30ng.org/v1` stays; https, certificate verification,
   no redirects, no environment proxy and `DIVESAFE_LLM_ALLOWED_HOSTS` stay. No plain http, no
   IP-literal exception (the allow-list grammar already refuses IP literals).
2. **What the owner accepts.** All APPROVED knowledge may be sent to one named model host that
   passes prompts to a hosted vendor (today Anthropic through CLIProxyAPI), not only owner-written
   documents. **Every licensed document approved later also goes to that vendor**, so approval now
   also means "may be sent to the configured model host and the vendor behind it" (decision 8).
   Licence and `display_permitted` evidence stays waived for the MVP (ADR 0022 Known gap 31):
   whether a source's licence allows this rests on the owner alone. What is sent of an item is
   unchanged: only `KNOWLEDGE_VALUE_KEYS` (`agents/prompts.py`: `text`, `section`, `document_id`,
   `version`, `chunk`); attribution, licence, origin, the role labels and the hash never are.
3. **New egress basis `remote_knowledge_opt_in`, bound to the provider's real host.**
   - The adapter exposes a read-only `host` (the lowercased `hostname` it parsed and calls;
     `None` for providers with no server). `check_egress` returns the new basis only when **all**
     hold, and otherwise **raises** a fixed error (never falls back to `remote_external_opt_in`)
     whenever the knowledge host is set. The check runs before the exact-fake-provider short-cut,
     so a fake provider with the host set also raises:
     - `provider.external` is true and `DIVESAFE_ALLOW_EXTERNAL_LLM=true`;
     - `DIVESAFE_LLM_SERVER_IS_LOCAL` is not set;
     - `DIVESAFE_LLM_KNOWLEDGE_HOST` is exactly one host in the allow-list grammar (no comma, no
       trailing dot), is not `localhost` or `*.localhost`, and is in `DIVESAFE_LLM_ALLOWED_HOSTS`;
     - it **equals `provider.host`** and the host of `DIVESAFE_LLM_BASE_URL`; if either is
       `None` (possible only when validation was skipped), `check_egress` raises.

     `check_egress` repeats each check itself, because `model_copy` and `model_construct` skip
     Settings validators (the precedent in `models/egress.py`).
   - **Settings validation** refuses the knowledge host, in every environment, with fixed messages
     that do not echo the value: without the opt-in; with `DIVESAFE_LLM_SERVER_IS_LOCAL`; with
     `llm_provider=fake` or no base URL; off-grammar, more than one host, or a loopback name;
     outside the allow-list; not equal to the base URL host. A mismatch therefore **refuses
     start-up** (owner answer 2).
   - Absent or empty: exactly today's behaviour (`remote_external_opt_in`, all withheld).
   - The name sits beside `remote_external_opt_in` and says what it is: an opt-in, not a claim about
     the host. A host name, not a boolean, so pointing the base URL elsewhere never carries the
     acceptance (start-up refuses instead). The port is not bound: another port on the same named
     host would be covered, which on a host the owner runs is accepted (Known gaps).
   - **Loopback is not covered** (owner answer 3): `loopback_external_opt_in` keeps withholding.
     On loopback a name binds nothing (port squatting, tunnels; ADR 0023); a named https host is
     bound by the allow-list and its certificate.
   - The one producer of the value is `check_egress`. `record_egress` accepts it as a string from
     any caller, as it does every basis; the record checks below are what hold for all callers.
   - This answers ADR 0023's "trusted host" question for knowledge only and only for the host named;
     a self-hosted model on another machine can use the same setting.
4. **The record.** `RecordEgressBasis` and `EgressBasis` gain `remote_knowledge_opt_in` (the record
   enum stays "gate values plus `no_agent_stage`"). `_check_egress` allows unwithheld `kb:` items
   under `declared_local` or `remote_knowledge_opt_in` only; its fixed message names both. Under
   the new basis `model_host` must be a non-loopback DNS name, **not an IP literal** (the value is
   not in `_LOOPBACK_BASES`, so the existing cross-check refuses a loopback host; the IP refusal is
   new and applies to this basis only). Older records are unaffected.
   - **What the record shows was sent:** the record has no `sent_evidence_ids` field (that lives on
     the stage result and the run, where Supervise reads it). The knowledge that reached prompts is
     the `kb:` evidence minus `withheld_knowledge`, read with `egress_basis` and `model_host`.
     `withheld_knowledge` is recomputed with the same filter (`pipeline.py`), so the two cannot
     disagree; a test pins this for the new basis.
   - **Updated invariant, checkable from the record alone:** a record whose evidence holds a `kb:`
     item that is not withheld has `egress_basis` of `declared_local`, or `remote_knowledge_opt_in`
     with a non-loopback, non-IP `model_host`.
   - The record cannot show that `model_host` was the configured knowledge host (Known gaps).
5. **The filter.** `split_for_egress` sends knowledge only when `type(basis)` is exactly `str` or
   `RecordEgressBasis` and the basis is one of the two values (a frozenset of exact strings; no case
   folding; any other type, including a `str` subclass, withholds rather than raising). A
   malformed `kb:` id is never sent. Applied once in `run_agent_stage`, as now.
6. **What the human sees.** The guide stays in the view (text lookup unchanged). The view (in
   `api/schemas.py`, beside the existing knowledge notes; no new `KnowledgeNoteCode`, so no record
   change) adds one fixed sentence, "Site document extracts were included in prompts to the external
   model server.", only when the basis is `remote_knowledge_opt_in`, the record holds unwithheld
   `kb:` evidence, and the Reason stage ran. No host is shown; `model_host` stays hidden from
   create-only keys. The existing withheld sentence ("because the model server is not declared
   local") becomes "because the model server is not declared local or named for site documents".
7. **The store records the attestation, and start-up checks it (owner answer, second round).**
   - **Migration 6** adds `kb_documents.attests_model_host_egress boolean NOT NULL DEFAULT false`.
     Only the loader sets it, from the manifest (decision 8), through its INSERT (the loader's
     table-level INSERT grant covers the column; no grant in a migration); existing rows, including
     Tioman v2, read `false`. The append-only triggers keep the row write-once. Its SQL must pass
     `guarded()` (pinned by a test). The knowledge shape pins (`services/migrations.py`, the
     `kb_documents.columns` digest for PostgreSQL 16 and 17) are regenerated with `kb_pins` on
     fresh scratch databases, never read from the database being checked, reviewed, and confirmed
     by the live pin test; the "regenerate when migration 4 changes" comment becomes "migration 4
     or a later knowledge migration". Without this, `migrate()`'s post-check rolls step 1 back.
   - **Start-up check, independent of dates and sites.** Under `remote_knowledge_opt_in`, start-up
     refuses (fixed message, no document text) while any version whose latest status is APPROVED
     and that has no WITHDRAWN event has `false`, whatever its dates. This is stricter than the
     in-force query: a later-dated or expired unattested version must be withdrawn too. If the store
     cannot be read (connection, timeout, malformed row), start-up refuses; it never passes. With
     the knowledge host set and no knowledge store configured (outside production only), start-up
     refuses too.
   - **Run-time retrieval rule** (approvals and withdrawals happen without a restart). The exclusion
     switch is **each run's recorded basis**: `assess_dive` passes `run.egress_basis` (set before
     Observe) to `knowledge.retrieve(..., egress_basis=...)` on every call, and the exclusion
     applies when it equals `remote_knowledge_opt_in`. It is never bound once at start-up and never
     a second read of the setting, so a caller cannot pair the new basis with a connector that does
     not exclude. Under that basis the governing version is chosen **exactly as today** (no
     fall-back to an older attested version); if it is unattested, its whole family returns nothing
     and the connector reports a fixed issue `knowledge store: a document version is not approved
     for the model host`. Like every evidence issue it becomes an INSUFFICIENT EVIDENCE rule result,
     so while a site's governing version is unattested every assessment for that site is
     INSUFFICIENT EVIDENCE until the version is withdrawn or replaced; it never produces GO.
     The rule is applied identically in SQL (`chosen` in
     `rag/store.py`) and in code (`attests_model_host_egress` added to `_VERSIONS`, `VersionRow`
     and `version_row`; the family classified in `rag/selection.py`), so the connector's
     disagreement check stays quiet. Under every other basis the query is unchanged.
   - This is a retrieval rule applied the same way to every document under this basis, not the
     per-document runtime egress flag ADR 0022 rejected (alternative C below).
8. **Approval meaning (owner answer 4: option B).** The manifest gains a required attestation
   `attests_model_host_egress: true`, refused otherwise like the two existing ones, so it is inside
   the SHA-256 the dive professional approves. The manifest `format` becomes 2, so a format-1 draft
   is refused for a clear reason (`kb_manifest`, `kb_manifest_build`, the draft field list in
   `docs/knowledge-loader.md`). The runbook says approval means "may be
   shown to every key holder and may be sent to the configured model host and its vendor".
   Production holds `tioman-site-guide` v2 APPROVED (v1 WITHDRAWN), without the attestation. The
   guide reaches the vendor only after a v3 with the attestation is loaded and approved and v2 is
   withdrawn; until then start-up refuses with the variable set (decision 7).
   - Alternatives: A. runbook text plus a fixed token in the approval record `notes` (process
     only); C. a per-document runtime flag (rejected in ADR 0022); D. this ADR alone (the approver
     is never told).
9. **PHN is unchanged (owner answer 5, second round).** In code the API never withholds the PHN
   item from any model; only the start-up warning in `build_provider` (`phn_etide_enabled and basis
   != "declared_local"`) and the evaluation rule `tide_check_allowed` (`declared_local` or
   `DIVESAFE_EVAL_ACCEPT_PHN_TERMS_OPEN=true`) look at the basis. **Both stay exactly as they
   are**: under `remote_knowledge_opt_in` the warning still fires when PHN is on, and the evaluation
   tide check still needs the explicit acknowledgement, because PHN's terms are not on file and the
   knowledge attestation does not cover PHN data. PHN stays off in production (ADR 0021).
   - **Evaluation gate.** `egress_gate` gains a `knowledge_host` argument and builds its `Settings`
     with `llm_provider="openai_compatible"`, `llm_base_url` set to the evaluation URL,
     `llm_allowed_hosts` and the knowledge host (no `model_construct`), so every Settings and
     `check_egress` refusal applies, including the `provider.host` comparison. A `RuntimeError`
     from `check_egress` skips with its existing fixed message; a `ValidationError` is **not**
     caught and fails the run, as `test_both_flags_together_fail_instead_of_skipping` requires.
10. **Unchanged.** The agent stage only tightens; `reconcile` unchanged; no rule reads KNOWLEDGE;
    Supervise checks citations against the ids sent; the 20-item bound, screens and delimiting apply.

## Consequences

- Licensed site text reaches a third-party vendor through a host whose forwarding the software
  cannot see: the owner trades confidentiality and licence risk for more context for the agents.
  The recommendation still cannot fall below the rules; explanation text a human reads can still be
  steered by a passage (Known gaps).
- Prompts grow by up to 20 chunks: tokens, cost, latency, more replies at the output cap. A failed
  call degrades to "no proposal"; the deterministic result stands.
- **Turning it off** is unsetting the variable and restarting; stored records stay readable.
  Knowledge already sent, and anything the proxy or vendor logged, cannot be recalled.
- **Deploy and rollback (owner answer 7).** Migration 6 makes an older image refuse to start ("the
  database is ahead of this code"), and an older image cannot load a record with the new basis
  (enum refusal). Deploy in steps: (1) the image with migration 6, the variable absent (behaviour
  unchanged); (2) load and approve Tioman v3 with the attestation, withdraw v2, with an ADR 0024
  record and backup; (3) set the variable and restart. Rolling back past step 1 needs a restore
  (ADR 0024).
- AGENTS.md section 12, ADR 0024 ("What leaves the machine", "The API and knowledge on this
  stack"), `docs/architecture.md`, ADR 0022's invariant (decision 15) and the code docstrings that
  say "only under `declared_local`" (`RecordEgressBasis`, the record fields, `_check_egress`) get a
  line each when this is built.
- ADR 0022 Known gap 25 (knowledge half of the citation guard) matters the first time a ruleset is
  signed, now that knowledge reaches a hosted prompt.

## Tests (test-engineer; safety-marked unless noted)

Allowed and refusing bases are written as **literals** in the tests, never derived from sets
imported from the code, so a code change cannot move the test with it.

- **Record validator:** unwithheld knowledge accepted under the new basis with a remote DNS host;
  refused with a loopback host, an IP-literal host or no host; still refused under every other basis
  and absent; withheld knowledge still uncitable; old records load; a literal six-value check of
  `RecordEgressBasis`. Extend `test_a_loopback_basis_needs_a_loopback_host` and the remote-host test
  with the new basis.
- **Filter and stage:** the new basis sends every well-formed `kb:` item; near misses (case,
  trailing space, `"RecordEgressBasis.REMOTE_KNOWLEDGE_OPT_IN"`, non-string, unhashable, a `str`
  subclass instance equal to `"remote_knowledge_opt_in"`) withhold
  without raising; `remote_external_opt_in` still withholds; malformed ids never sent; prompts hold
  the chunk text and no attribution or licence value; the record's `withheld_knowledge` matches
  the stage for the new basis.
- **Pipeline (`record_egress`):** new basis with a host keeps knowledge, `withheld_knowledge == ()`,
  round-trips; with no host gives `none`, `egress.host_missing`, everything withheld; with
  `localhost` is refused by the record.
- **`check_egress`, provider and Settings:** each refusal in decision 3 through normal validation,
  `model_copy` and `model_construct`; provider host differs from the knowledge host while the
  Settings host matches → refused; comma lists, trailing dot, upper-case base URL, `localhost`,
  fake provider, no base URL, and (through `model_construct`) `llm_base_url` or `provider.host`
  `None` → raises; empty means absent (`env_ignore_empty`); messages carry no host or
  URL.
- **Migration and pins:** `guarded()` accepts migration 6's exact SQL; migration 6 adds the column
  with `false` for existing rows; the regenerated 16/17 pins pass the live pin test and `verify()`
  on a migrated database; an older image refuses a migrated database.
- **Loader and manifest:** format 1 is refused; format 2 without the attestation, or with it
  `false`, is refused; a loaded version stores `true`.
- **Start-up:** with the knowledge host set, refused while an APPROVED, non-withdrawn version is
  `false` (including one dated in the future or expired); refused when the store cannot be read
  or no store is configured;
  passes once every such version is `true`; unaffected when the variable is absent.
- **Retrieval:** under the new basis an attested governing version is returned; v3 attested plus v4
  approved without the attestation returns nothing from that family, the fixed issue, and no
  disagreement issue (no fall-back to v3); SQL and code agree; under `remote_external_opt_in` the
  query is unchanged and everything is withheld; one connector serves runs with either basis
  and excludes only for the run whose recorded basis is `remote_knowledge_opt_in`.
- **API:** `build_provider` with the new basis; the start-up log (host name only); the view sentence
  only under its three conditions; the reworded withheld sentence; create-only keys never see the
  host.
- **Compose:** `DIVESAFE_LLM_KNOWLEDGE_HOST: ${DIVESAFE_LLM_KNOWLEDGE_HOST:-}` exactly, on `api`
  only (not `db-init` or the `kb_*` services); `production.env.example` has the empty line.
- **PHN unchanged:** under the new basis the PHN egress warning still fires and
  `tide_check_allowed` still refuses without the acknowledgement (strengthens the existing tests).
- **Evaluation (offline):** `egress_gate` returns the new basis only when the knowledge host equals
  its evaluation URL's and provider's host; otherwise a `RuntimeError` skips with the fixed
  message and a `ValidationError` (for example a host mismatch) fails the run;
  `test_the_live_evaluation_setup_runs_the_gate` gains the new basis.
  `capture_item6.EGRESS_BASES` gains the new label and its "four fixed labels" docstring changes.
- **Mutation checks:** filter treats any basis as local; filter folds case; filter drops the
  exact-type guard; validator allows `remote_external_opt_in`; validator drops the loopback or
  the IP-literal refusal; `check_egress` skips host equality, the provider-host comparison,
  allow-list membership, the opt-in, the IS_LOCAL refusal or the loopback refusal; Settings skips
  each refusal; start-up skips the attestation check or passes when the store is unreadable;
  the exclusion in SQL only, in code only, or before the governing version is chosen; the
  exclusion keyed on the setting or a start-up value instead of the run's basis;
  `assess_dive` not passing the basis to `retrieve`; the gate catching `ValidationError`;
  the manifest accepts format 1;
  non-empty compose default; PHN warning or tide rule widened to the new basis; view sentence
  dropped or shown without its conditions.

### Existing tests that need amending (owner-approved 2026-11-14, AGENTS.md section 4)

Each keeps a mutation check, re-pointed where its edit target moves; none is skipped or deleted.

- `tests/safety/test_record_egress.py`: `test_knowledge_not_withheld_is_refused_unless_declared_local`
  and `test_one_unwithheld_item_among_several_is_refused`; `test_mutation` ids `validator-dropped`,
  `none-sends`, `no-agent-stage-sends`, `loopback-opt-in-sends` (message text); constants
  `NOT_DECLARED_LOCAL` and `HOST` become literals; `test_knowledge_without_a_kb_id_is_refused_by_the_record`
  (mechanical); `test_a_model_server_basis_needs_its_host` and the enum-equality test (strengthen);
  module docstring.
- `tests/agent/test_knowledge_egress.py`: `test_mutation_of_the_stage_filter_is_caught` param
  `any-basis-is-local` (edit target changes); `WITHHOLDING` gains near misses (strengthens);
  docstring.
- `tests/safety/test_production_compose.py`: `test_the_api_gets_exactly_these_settings` (one new
  setting) and the stale comment that "without the declaration every knowledge item is withheld".
- `tests/safety/test_llm_egress_declaration.py` and `tests/evaluation/test_egress_gate_offline.py`:
  no relaxation; PHN and gate cases for the new basis are added (strengthen).
- The knowledge pin tests and the manifest/loader tests: pins regenerated and format 2 (mechanical,
  reviewed).

## Reviews

`security-reviewer` and `risk-reviewer`, on this ADR before code and on the code after.

## Known gaps

- **The software cannot verify where the host forwards.** The setting names the first hop; the
  proxy's routing and `DIVESAFE_LLM_MODEL` can send knowledge to another vendor with no change here
  (owner answer 6: no model pin; the runbook says a vendor change needs a new decision). The
  record's model name is the one requested.
- **Vendor retention and training terms are not on file**, for the proxy or for Anthropic (owner
  answer 8).
- **Licence evidence stays waived** (ADR 0022 Known gap 31); the attestation is a statement.
- **The record cannot show the host was the configured knowledge host**; it shows a remote DNS
  name under the opt-in basis. `record_egress` trusts the basis string it is given, as it does for
  `declared_local`.
- **The port is not bound**, only the host name.
- **Plans name only the island**, so site-specific hazards cannot be matched to a dive site; named
  dive sites are separate work.
- A passage can still steer explanation text a human reads; a hosted model is now a second reader.
- The trial is a handful of runs with unmerged code; the evaluation harness has not measured
  knowledge with a hosted model (ADR 0022 Known gap 21 applies when it does).
- Settings are read at start-up; a change needs a restart.
- **Review nits for the build (round 4, both reviewers):** the new `egress_basis` argument of
  `KnowledgeConnector.retrieve` and `KnowledgeStore.select` should be keyword-only with no
  default, and `fetch` should take it too or always exclude (the mutation then reads "passes the
  wrong basis"); the knowledge store and connector test fakes (`FakeStore` in
  `tests/agent/test_knowledge_connector.py`) need the argument (mechanical).

## Owner answers (2026-11-14)

1. Basis name: `remote_knowledge_opt_in`.
2. A knowledge host that does not match the base URL host refuses start-up.
3. `loopback_external_opt_in` keeps withholding.
4. Approval meaning: option B (manifest attestation `attests_model_host_egress` plus runbook text).
5. First round: "include PHN" as the warning and tide gate. **Second round, after review: PHN is
   unchanged**; the warning and `tide_check_allowed` keep their current rules.
6. The model name is not pinned; the runbook says a vendor change needs a new decision.
7. The stepped deploy, and a restore to roll back past the first image, are accepted.
8. Vendor retention and training terms: accepted as a Known gap for now.
9. The AGENTS.md section 12 line and the listed safety-test amendments are approved; each amended
   test keeps a mutation check, and none is skipped or deleted.
10. Second round: the attestation is stored per document version (migration 6), and start-up
    refuses with the knowledge host set while any APPROVED, non-withdrawn version lacks it,
    whatever its dates (decision 7).
