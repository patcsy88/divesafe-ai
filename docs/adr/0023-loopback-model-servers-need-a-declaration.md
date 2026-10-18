# ADR 0023: A loopback model server needs the operator's declaration

Status: PROPOSED (2026-10-18). Needs `security-reviewer` and `risk-reviewer` (it touches config,
the egress gate and `tests/safety/`). Amends ADR 0007, 0016 and 0018. Closes the existing gap
recorded in ADR 0022 decision 15.

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
   proxy let through by the opt-in is not logged as local. Recording it in `model_version` and
   using it in the ADR 0022 knowledge filter belong with ADR 0022 and are not done here; that
   filter must use the basis, not `external`.
7. **Live evaluation** runs the same `check_egress` (via `tests/evaluation/egress_gate.py`), skips
   when it refuses, fails on the contradictory pair, and writes the basis into both output files.
   It reads the two flags through `Settings`, so `1`, `TRUE` and `true` mean what they mean at
   start-up. Its tide check also skips unless the basis is `declared_local` or
   `DIVESAFE_EVAL_ACCEPT_PHN_TERMS_OPEN=true` is set, because the opt-in covers where the prompt
   goes, not the licence of the data in it (PHN's terms are not on file, ADR 0021).
8. **Callers.** `create_provider` and the adapter constructors do **not** check egress. Every
   runtime caller must go through `build_provider` or call `check_egress` itself and log the
   basis. A safety test scans `src/` and fails if `create_provider` or an adapter constructor is
   called from anywhere else (`models/` and `api/state.py` only). The check is a text scan, not a
   proof: an alias or a dynamic lookup would pass it.

## Consequences

- **Behaviour change.** Anyone running Ollama or an OpenAI-compatible server on loopback must now
  set `DIVESAFE_LLM_SERVER_IS_LOCAL=true` (or the opt-in) or the application will not start.
  `.env.example` says so. The `fake` provider is unaffected.
- A proxy such as the one used on 2026-10-17 now needs the operator to say
  `DIVESAFE_ALLOW_EXTERNAL_LLM=true`, which is the true description of it.

## Tests

- `tests/safety/test_llm_egress_declaration.py`: undeclared loopback refused; declaration or
  opt-in accepted (also through the environment variable, including an empty value); declaration
  refused for a remote server; both settings together refused; only the exact fake class exempt;
  real adapters on loopback need it; `build_default_state` goes through the gate; the basis for
  each case; the start-up log records `egress_basis` and `address_is_remote` for a loopback and a
  remote case; no source file outside the start-up path calls `create_provider` or an adapter
  constructor; the live-evaluation setup runs the gate (checked offline by patching the module's
  settings, since the module itself is skipped without a model).
- `tests/evaluation/test_egress_gate_offline.py` (safety): the evaluation gate refuses what
  start-up refuses.
- `tests/unit/test_loopback_peer.py` (peer check over http and https for both adapters, IPv4-mapped
  addresses, Ollama sending nothing when the peer is not loopback, Ollama's TLS context) and the
  TLS-context test in `test_openai_compatible_transport.py`, both marked safety. These use
  `.invalid` host names, which never resolve, so they cannot make a real call.
- **Mutation checks.** The first test set was mutation-checked with nine mutations (each gate
  removed, the peer check removed, the https handler swapped in each adapter, Ollama's loopback
  flag dropped, the exemption widened to subclasses); two survived and were closed with tests.
  A later round applied 16 and caught 14; the two survivors were the IPv4-mapped conversion and
  the evaluation gate call. The last round applied five: the evaluation gate call, a stray
  `create_provider` caller, a stray adapter constructor, a dropped `address_is_remote`, and a
  strict flag parser in the evaluation gate. All five are caught. The last one survived at first,
  because a skip inside the test hides as a skip, and was closed by making the positive cases
  fail on a skip. One survivor remains, listed below.

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
  service), and secrets mounted as files or passed one by one, not the whole `.env`. None of that
  is done.
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
    conservative (it can only withhold more), and it will matter for ADR 0022's rule that only
    `egress_permitted` documents go to an external provider. Whether a "trusted host"
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
- **One mutation survives, and no current Python can catch it.** Removing the IPv4-mapped
  conversion in `is_loopback` changes nothing on Python 3.14.8 or on 3.11.17 (checked on both on
  2026-10-18 in a throwaway environment: the 63 loopback and egress tests pass with and without
  the conversion). Both standard libraries already treat `::ffff:127.0.0.1` as loopback inside
  `IPv6Address.is_loopback`, so the conversion is redundant there. An earlier note said CI's 3.11
  would catch it; that was wrong for current patch releases. The conversion only matters on an
  older patch release that lacks the standard-library handling, which was not available to test.
  Without it a mapped loopback address would be refused, which fails closed, and a mapped
  non-loopback address is refused by the standard library on every version checked. The
  parametrised test stays as a guard against the standard library changing. The evaluation gate
  call, a survivor in the previous round, is now caught offline.
- **No test uses a real loopback TLS server**, so hostname matching and "nothing sent" over https
  are covered only through the patched connection class.
- **The opt-in covers the egress decision, not the licence of the data sent.** The recorded PHN
  tide item has gone to a third-party host in two live runs while PHN's terms are not on file
  (ADR 0021). The live tide check now skips unless the basis is `declared_local` or an explicit
  acknowledgement is set; nothing equivalent exists for a running application.
- **The basis cannot tell hosts apart.** A self-hosted model on another machine and a public
  forwarding proxy both log `remote_external_opt_in`, so setting the opt-in becomes routine and
  signals less. It does not name the recipient. `model_version` should record the host too,
  because one opt-in covers every allow-listed host and model.
- **The basis is not on the assessment record**, so a reviewer of a past assessment cannot see
  whether its data left the machine. The model name on a record is the name requested; behind a
  routing host it does not show which model answered.
- **Confidentiality only.** An unverified upstream can return anything. Authority design limits
  this to adding caution or noise, but its explanation prose still reaches the human decider
  (ADR 0007's plain-text and untrusted-prose rules apply).
- **The declaration is a statement the software cannot check.** An operator can declare a
  forwarding proxy local. The control is that the choice is explicit, default-off and logged, not
  that it is verified.
- The peer check sees the first TCP peer only. A loopback server that forwards is still allowed
  if declared.
- `model_version` does not yet record the declaration (ADR 0022 decision 15).
- Settings are read once at start-up; changing the variable needs a restart.
- Hosted vendor adapters (`openai`, `anthropic`) are not built and each needs its own ADR.
