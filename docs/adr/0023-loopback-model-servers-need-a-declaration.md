# ADR 0023: A loopback model server needs the operator's declaration

Status: ACCEPTED by the owner (2026-10-20); originally PROPOSED (2026-10-18). Needed `security-reviewer` and `risk-reviewer` (it touches config,
the egress gate and `tests/safety/`). This line records only that those reviews were needed and
that the owner accepted it on 2026-10-20; the build reviews are in the conversation record and the
commit messages, not in a history section. Amends ADR 0007, 0016 and 0018. Closes the start-up part
of the gap recorded in ADR 0022 decision 15. **ADR 0022 requires typed record fields `egress_basis`
and `model_host` (not a string in `model_version`); they are not yet built**, and using the basis
in the knowledge filter is not done either.

## Context

`LLMProvider.external` was true only when the model server's host was not loopback. AGENTS
section 12 says hosted providers need the explicit opt-in `DIVESAFE_ALLOW_EXTERNAL_LLM`. But a
server on `localhost` can forward every prompt to a hosted model: the proxy at `localhost:8317`
used in the 2026-10-17 live run did exactly that. It counted as local, skipped the opt-in, and
plan and evidence data left the machine. The address cannot show where the prompt goes.

## Decision

1. **A loopback server is refused at start-up unless the operator says what it does.** One of:
   - `DIVESAFE_LLM_SERVER_IS_LOCAL=true`: the model runs on this machine and the server forwards
     nowhere; or
   - `DIVESAFE_ALLOW_EXTERNAL_LLM=true`: the operator accepts that prompts may leave the machine.

   Both default to false, so the default is refusal. `build_provider` calls
   `divesafe.models.egress.check_egress`, which returns the **egress basis** (`none` for the
   exact fake class, `declared_local`, `loopback_external_opt_in`, `remote_external_opt_in`).
   `provider.external` now means only "the address is not on this machine"; it does not show where
   the prompt goes.
2. **The declaration is never inferred** from the URL or the provider, and the two settings
   contradict each other, so setting both fails at settings validation.
3. **A remote server** (`external` true) still needs `DIVESAFE_ALLOW_EXTERNAL_LLM=true`, and the
   declaration is **refused** with it: a server that is not on this machine cannot be "local".
   Start-up fails with a fixed message.
4. **Only the exact `FakeProvider` class is exempt** (it sends nothing, with the assumption in
   "Not solved"). A subclass is not, so a test double cannot carry the exemption into a real
   provider.
5. **The peer check covers https and Ollama.** Before this change only the OpenAI-compatible
   adapter checked, and only over plain http, that the connected address was loopback. The check
   now lives in `divesafe.models.loopback` and covers http and https for both adapters, closing
   the connection before the prompt or an API key is sent. Over http the check runs right after the
   TCP connection; over https it runs after the TLS handshake, so the ClientHello and server name
   have already gone but no request body or key has. IPv4-mapped IPv6 peers are judged as the
   IPv4 address they map. It proves the TCP peer is on this
   machine. It does not prove where that peer forwards the prompt; the declaration does that.
6. **The basis is logged** at start-up (`egress_basis`, with `address_is_remote`), so a forwarding
   proxy let through by the opt-in is not logged as local. Recording it on the assessment record
   (as the typed fields `egress_basis` and `model_host` that ADR 0022 decision 15 requires, not
   inside `model_version`) and using it in the ADR 0022 knowledge filter belong with ADR 0022 and
   are not built; that filter must use the basis, not `external`.
7. **Live evaluation** runs the same `check_egress` (via `tests/evaluation/egress_gate.py`), skips
   when it refuses, fails on the contradictory pair, and writes the basis into both output files.
   It reads the two flags through `Settings`, so `1`, `TRUE` and `true` mean what they mean at
   start-up. Its tide check also skips unless the basis is `declared_local` or
   `DIVESAFE_EVAL_ACCEPT_PHN_TERMS_OPEN=true` is set (the exact text `true` only), because the
   opt-in covers where the prompt goes, not the licence of the data in it (PHN's terms are not on
   file, ADR 0021). The rule is the function `tide_check_allowed` and is tested offline. A skip
   writes `tide_check_<provider>.json` with `status: skipped`, the reason and the basis; a run
   writes `status: ran` and whether the acknowledgement was set. Both output files record the
   host name only (no scheme, port, path or key).
8. **Callers.** `create_provider`, the adapter constructors, each adapter module's `_factory` and
   the registry do **not** check egress. Every runtime caller must go through `build_provider` or
   call `check_egress` itself and log the basis. A safety test scans each file under
   `src/divesafe` and fails if any of those names is **mentioned** (called, indexed, passed as a
   value, iterated) outside the files that define or re-export it (`models/` and `api/state.py`),
   and checks that the pattern still matches in those files (a definition counts, so this shows
   the pattern works, not that the files still go through the gate). A mention in a comment also
   fails it, which is safe. The check is a text scan, not a proof: an `as` alias, `getattr`,
   `functools.partial`, a second function inside an allowed file, or code outside
   `src/divesafe` passes it. A stronger design would have `check_egress` mark the provider and
   make each adapter refuse to send without the mark; it is not built.
9. **`declared_local` is not tied to the peer check.** `check_egress` returns it for any provider
   whose `external` is false. Today the only two adapters run the loopback peer check, but a
   future adapter with `external` false and no peer check would also get `declared_local`, which
   ADR 0022 treats as unfiltered. Each new adapter must do the peer check. A safety test now
   builds every registered real adapter on loopback and fails unless its opener uses the
   loopback http and https handlers, so a new adapter fails it until it does the check or the
   exception is written down here. That test sees the handlers, not that the peer check runs.

## Consequences

- **Behaviour change.** Anyone running Ollama or an OpenAI-compatible server on loopback must now
  set `DIVESAFE_LLM_SERVER_IS_LOCAL=true` (or the opt-in) or the application will not start.
  `.env.example` says so. The `fake` provider is unaffected.
- A proxy such as the one used on 2026-10-17 now needs the operator to say
  `DIVESAFE_ALLOW_EXTERNAL_LLM=true`, which is the true description of it.
- **Production cannot run without a model server.** Settings refuse `llm_provider=fake` in
  production. With the planned host, every production assessment sends its plan fields and
  evidence to a public host that probably forwards them, and also the PHN tide item whenever
  `DIVESAFE_PHN_ETIDE_ENABLED` is on. An operator who wants no data to leave the machine has no
  production option today except a model on loopback that they declare local, and **inside a
  container that exception hardly exists**: loopback is the app container itself, so the model
  would have to run in that same container, or the container would need host networking (which
  brings back the port-squatting exposure below and drops network isolation). A model in a
  sibling container is a named host and gets `remote_external_opt_in`. Until the owner decides,
  "production sends" is the decision actually in effect.
  - **What the decision is about.** The agent stage can only tighten a result, so the safety
    floor (the rules and `reconcile`) does not depend on it. The owner is trading
    confidentiality and data-licence risk against extra caution and explanation, not choosing
    between safe and unsafe output.
  - **Decided by the owner (2026-10-18): production cannot run with the agent stage off.** That
    is what Settings already enforce (`llm_provider=fake` is refused in production), and a safety
    test now pins it. The consequence stands as written above: every production assessment
    sends its plan fields and evidence to the configured model host. It is a decision to accept
    that, not a finding that it is free of risk. Development and test may still run with the
    stage off, so the question below still applies to them. This ADR does not check whether a
    record made with the stage off tells the human decider so (with the stage off,
    `model_version`, findings and agent issues are simply empty); that should be checked.
  - Whether the other sources' terms allow their data to be passed to a third-party model host is
    not stated here and was not checked. (ADR 0024 adds a start-up warning when PHN data is
    enabled and the basis is not `declared_local`, and the start-up log now names the host.)

## Tests

- `tests/safety/test_llm_egress_declaration.py`: undeclared loopback refused; declaration or
  opt-in accepted (also through the environment variable, including an empty value); declaration
  refused for a remote server; both settings together refused; only the exact fake class exempt;
  real adapters on loopback need it; `build_default_state` goes through the gate; the basis for
  each case; the start-up log records `egress_basis` and `address_is_remote` for a loopback and a
  remote case; no source file outside the start-up path mentions `create_provider`, an adapter
  constructor, a module `_factory` or the registry, and the pattern is tested on its own; every
  registered real adapter uses the loopback handlers; the live-evaluation setup runs the gate
  (checked offline by patching the module's settings, since the module itself is skipped without
  a model); the live tide check sends nothing without the acknowledgement and leaves a skip
  record without replacing an earlier file; it runs, and records `status: ran` and the
  acknowledgement, when declared local or acknowledged; a placeholder key never appears in either
  record. The live module's `git rev-parse` call is patched out, so these tests start no
  subprocess.
- `tests/evaluation/test_egress_gate_offline.py` (safety): the evaluation gate refuses what
  start-up refuses, with skips matched on their reason; the tide-check rule for all eight
  combinations; an output path never replaces an existing file.
- `tests/unit/test_loopback_peer.py` (peer check over http and https for both adapters, IPv4-mapped
  addresses, Ollama sending nothing when the peer is not loopback, Ollama's TLS context) and the
  TLS-context test in `test_openai_compatible_transport.py`, both marked safety. These use
  `.invalid` host names, which are reserved never to resolve (RFC 6761). Some resolvers rewrite
  failed lookups, so the second safeguard is that no public authority issues a certificate for
  `.invalid`: certificate verification fails before any request body is sent.
- **Mutation checks.** Earlier rounds applied 9, then 16, then 5 mutations as the code changed.
  After the last review all of them were **re-run on the final code, with more added**: 34 in
  total, **33 caught, 1 survivor** (listed under "Not solved"). The 27 numbered ones, each applied
  alone and run against the egress, loopback, adapter and agent safety tests:
  1. the loopback gate in `check_egress` removed; 2. the declaration allowed for a remote server;
  3. the fake exemption widened to subclasses; 4. the remote opt-in removed; 5. the
  both-flags contradiction removed; 6. the peer check removed; 7. the https handler in the loopback
  module swapped for the stock one; 8. Ollama's loopback flag dropped; 9. the OpenAI-compatible
  https handler swapped; 10. Ollama's https handler swapped; 11. `address_is_remote` dropped from
  the log; 12. `egress_basis` dropped from the log; 13. `build_provider` skipping `check_egress`;
  14. the evaluation gate call replaced; 15. a strict flag parser in the evaluation gate;
  16. the tide rule always allowing; 17. the tide rule ignoring the acknowledgement; 18. the tide
  rule treating the loopback opt-in as local; 19. the tide check skipping the rule;
  20. the OpenAI-compatible http handler swapped; 21. Ollama's http handler swapped; 22. the
  output path overwriting an existing file; 23. the run record dropping the acknowledgement;
  24. the run record dropping its status; 25. the skip record host being the whole URL; 26. Ollama
  using no loopback handlers at all; 27. the IPv4-mapped conversion removed (the survivor). Seven
  more put a stray file under `src/` that uses `create_provider` (called, and passed as a value),
  an adapter constructor, a module `_factory`, and the registry (indexed, `.get`, iterated).
  Two earlier survivors were closed by making positive cases fail on a skip (a skip inside a test
  hides as a skip) and by the offline setup test for the evaluation gate.

## Not solved

- **`docker/docker-compose.yml` is a development stack, not a deployment target.** It calls
  itself one. It gives the app container the owner-role `DIVESAFE_ADMIN_DATABASE_URL` (which
  `.env.example` says is never given to the app), passes the whole `.env` through `env_file`
  (so the model API key, `POSTGRES_PASSWORD`, the MET token and the API key hashes sit in the
  container environment, visible in `docker inspect` and `/proc/<pid>/environ`), and has no
  `sslmode` on the database URL, so `environment=production` refuses to start and a stack built
  from it runs as `development` without the production checks. **A production deployment needs
  its own compose file and ADR**: `environment=production`, a least-privilege database role with
  `sslmode=require`, no admin URL in the app container (run `db_init` as a separate one-off
  service), and secrets mounted as files or passed one by one, not the whole `.env`. ADR 0024
  (proposed) describes this; its start-up warning for PHN data with a non-local basis and the
  host name in the start-up log close two items listed above as missing. It has been
  started on a developer machine, with throwaway secrets (no assessment was run).
- **Containers and the intended model host (owner, 2026-10-18).** DiveSafe AI is to be deployed
  in Docker. The model host is `https://wall-e.ch30ng.org`. What that server is, and where it
  sends prompts, is **not known to this ADR**. A model name such as `claude-sonnet-5-5`, whose
  weights are not distributed, means the host either forwards to that vendor or maps the name to
  some other model. Consequences, checked against the code:
  - Inside the container, loopback is the container itself, so `localhost` can never reach a
    model elsewhere. A named host is not loopback, so the provider counts as remote (`external`
    true): it needs `DIVESAFE_ALLOW_EXTERNAL_LLM=true`, the declaration
    `DIVESAFE_LLM_SERVER_IS_LOCAL` is refused, and the egress basis is `remote_external_opt_in`.
  - **A model the operator knows forwards nowhere still cannot be declared local** unless it is
    on loopback. A self-hosted model on another machine is treated like a hosted one. That is
    conservative (it can only withhold more), and it will matter for ADR 0022's rule (decision 15) that
    corpus text reaches a model only when the basis is exactly `declared_local`, so a self-hosted
    model on another machine gets no knowledge items. Whether a "trusted host"
    declaration should exist is an open owner decision; it is not built.
  - The host must be listed in `DIVESAFE_LLM_ALLOWED_HOSTS` (`wall-e.ch30ng.org`).
  - It is https, which the OpenAI-compatible adapter requires for a non-loopback host. The
    adapter verifies the certificate and host name with `ssl.create_default_context()`, so the
    server needs a certificate the container's default trust store accepts. Never disable
    verification to get past a certificate error.
  - **Settings:** `DIVESAFE_LLM_PROVIDER=openai_compatible`, `DIVESAFE_ALLOW_EXTERNAL_LLM=true`,
    `DIVESAFE_LLM_ALLOWED_HOSTS=wall-e.ch30ng.org`, no `DIVESAFE_LLM_SERVER_IS_LOCAL`, and
    `DIVESAFE_LLM_BASE_URL` set to the server's API root. The adapter appends `/chat/completions`
    to the base URL's path, so whether the root is `https://wall-e.ch30ng.org` or ends in `/v1`
    is **not yet known**. The model name (`DIVESAFE_LLM_MODEL`) and the API key (environment
    only) are also needed.
  - **One unauthenticated check (owner-approved, 2026-10-18, from the developer machine, not a
    container):** `GET https://wall-e.ch30ng.org/v1/models` with certificate verification on,
    no key, no prompt data. Result: TLS verified, HTTP 401 with a JSON body, and response header
    names consistent with a routing proxy, so it probably forwards prompts (inferred from the
    headers, not confirmed). The name resolved to a public address, so this host is on the
    internet, not on the LAN. The 401 shows the path `/v1/models` exists and wants a key; it does not show that
    `/v1/chat/completions` works, which nothing has called.
  - **Not checked:** that the name resolves from inside the container, that the container's
    trust store accepts the certificate, which models the key can use, and whether the server
    forwards prompts. Start-up checks configuration, not whether the server works: if DNS, the
    trust store or the base path is wrong in the container, start-up succeeds and every
    assessment records an agent-stage issue while the deterministic result stands.
  - Environment proxies are ignored by the adapters, so a network that needs an egress proxy
    makes every model call fail. That fails safe; it is not supported.
  - The certificate presented on 2026-10-18 expires on 2026-12-20. A lapse makes model calls
    fail the same way.
  - The peer check applies only to loopback hosts; for a named host the connected address is
    not verified beyond the allow-listed host name and the certificate.
- **Port squatting.** "On this machine" is not "run by the operator". On a shared host another
  local user can bind the model port while the server is down and receive prompts and the bearer
  key over http. https with a trusted certificate reduces this; nothing here prevents it.
- **Tunnels.** An SSH tunnel or `kubectl port-forward` puts a remote model behind a loopback
  address. Declaring it local would be wrong; the start-up message and `.env.example` name this.
- **Certificate trust** follows the environment (`SSL_CERT_FILE`, `SSL_CERT_DIR`), as before. A
  private CA would need either a combined bundle or a model-adapter-only setting; neither is built
  and neither is needed for a publicly trusted certificate.
- **The fake exemption** assumes the scripted replies are strings or a test-only callable. The
  `fake` setting returns before the check anyway.
- **One mutation survives, and it was not caught on any Python checked.** Removing the
  IPv4-mapped conversion in `is_loopback` changes nothing on Python 3.14.8 or 3.11.17 (checked on
  2026-10-18 in a throwaway environment: the 63 loopback and egress tests pass with and without
  it). Both standard libraries already treat `::ffff:127.0.0.1` as loopback inside
  `IPv6Address.is_loopback`, so the conversion is redundant there. **Python 3.12 (the container
  image) and 3.13 were not tested.** An earlier note said CI's 3.11 would catch it; that was
  wrong for the patch release tested. Which patch releases lack the standard-library handling was
  not checked. Without the conversion a mapped loopback address would be refused, which fails
  closed, and a mapped non-loopback address is refused on every version checked. The
  parametrised test stays as a guard against the standard library changing.
- **No test uses a real loopback TLS server**, so hostname matching and "nothing sent" over https
  are covered only through the patched connection class.
- **The opt-in covers the egress decision, not the licence of the data sent.** The recorded PHN
  tide item went to a forwarding host in all three recorded live runs (two through the local
  proxy, one through `wall-e.ch30ng.org`), all before this rule existed, while PHN's terms are
  not on file (ADR 0021). Sharing a "ran" tide-check record also redistributes that item. The live tide check now skips unless the basis is `declared_local` or an explicit
  acknowledgement is set; nothing equivalent exists for a running application.
- **The basis cannot tell hosts apart.** A self-hosted model on another machine and a public
  forwarding proxy both log `remote_external_opt_in`, so setting the opt-in becomes routine and
  signals less. It does not name the recipient. The record should carry the host too (the typed
  field `model_host`, ADR 0022 decision 15; not built), because one opt-in covers every
  allow-listed host and model.
- **The basis is not on the assessment record** (the typed `egress_basis` field required by ADR
  0022 is not built), so a reviewer of a past assessment cannot see whether its data left the
  machine. The model name on a record is the name requested; behind a
  routing host it does not show which model answered.
- **Confidentiality only.** An unverified upstream can return anything. Authority design limits
  this to adding caution or noise, but its explanation prose still reaches the human decider
  (ADR 0007's plain-text and untrusted-prose rules apply).
- **The declaration is a statement the software cannot check.** An operator can declare a
  forwarding proxy local. The control is that the choice is explicit, default-off and logged, not
  that it is verified.
- The peer check sees the first TCP peer only. A loopback server that forwards is still allowed
  if declared.
- The record does not yet carry the declaration: ADR 0022 decision 15 requires the typed fields
  `egress_basis` and `model_host`, which are not built (the basis is not put in `model_version`).
- **Git history.** Earlier local commits of this ADR and of `docs/evaluation-strategy.md`
  contained the model host's public IP address, a LAN address and the name of its proxy
  software. On 2026-10-18, before anything was pushed (`origin/main` held none of it), the 11
  unpushed commits were rewritten to remove them, so every commit hash in that range changed. A
  bundle of the old commits was kept outside the repository and holds the old text; delete it if
  that matters. Anything already copied from a pre-rewrite checkout is not covered, and neither is the
  provider id in the model line of `.opencode/agents/*.md`, which is already in `origin/main`.
- Settings are read once at start-up; changing the variable needs a restart.
- Hosted vendor adapters (`openai`, `anthropic`) are not built and each needs its own ADR.
