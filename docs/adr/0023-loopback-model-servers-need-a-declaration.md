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
4. **Only the exact `FakeProvider` class is exempt** (it sends nothing). A subclass is not, so a
   test double cannot carry the exemption into a real provider.
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

## Consequences

- **Behaviour change.** Anyone running Ollama or an OpenAI-compatible server on loopback must now
  set `DIVESAFE_LLM_SERVER_IS_LOCAL=true` (or the opt-in) or the application will not start.
  `.env.example` says so. The `fake` provider is unaffected.
- A proxy such as the one used on 2026-10-17 now needs the operator to say
  `DIVESAFE_ALLOW_EXTERNAL_LLM=true`, which is the true description of it.

## Tests

`tests/safety/test_llm_egress_declaration.py` (undeclared loopback refused; declaration or opt-in
accepted; declaration refused for a remote server; both settings together refused; only the exact
fake class exempt; real adapters on loopback need it) and `tests/unit/test_loopback_peer.py` (the
peer check over http and https, Ollama sending nothing when the peer is not loopback). Nine
mutations were applied (each gate removed, the peer check removed, the https handler swapped in
each adapter, Ollama's loopback flag dropped, the exemption widened to subclasses). Two survived
the first test set and were closed with tests; all nine are now caught.

## Not solved

- **Containers and the intended model host (owner, 2026-10-18).** DiveSafe AI is to be deployed
  in Docker (`docker/docker-compose.yml`). The model host is `https://wall-e.ch30ng.org`. What
  that server is, and where it sends prompts, is **not known to this ADR**: it may run the model
  itself or forward to a hosted one. Consequences, checked against the code:
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
    names consistent with a routing proxy that routes to upstream providers (so it
    probably forwards prompts, but that is inferred from the headers, not confirmed). The name
    resolved to a public address, so this host is on the internet, not on the
    LAN. The 401 shows the path `/v1/models` exists and wants a key; it does not show that
    `/v1/chat/completions` works, which nothing has called.
  - **Not checked:** that the name resolves from inside the container, that the container's
    trust store accepts the certificate, which models the key can use, and whether the server
    forwards prompts.
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
- **Two mutations survive.** (1) Removing the IPv4-mapped conversion in `is_loopback` is
  equivalent on Python 3.14, whose `is_loopback` already treats `::ffff:127.0.0.1` as loopback; the
  conversion matters on older Pythons (CI also runs 3.11) and the parametrised test would catch it
  there, but that was not run here. (2) Replacing the `egress_gate(...)` call in
  `test_evaluation_live.py` is not caught offline, because that module is skipped without
  `DIVESAFE_EVAL_MODEL`. The gate function itself is covered by `test_egress_gate_offline.py`.
- **No test uses a real loopback TLS server**, so hostname matching and "nothing sent" over https
  are covered only through the patched connection class.
- **The declaration is a statement the software cannot check.** An operator can declare a
  forwarding proxy local. The control is that the choice is explicit, default-off and logged, not
  that it is verified.
- The peer check sees the first TCP peer only. A loopback server that forwards is still allowed
  if declared.
- `model_version` does not yet record the declaration (ADR 0022 decision 15).
- Settings are read once at start-up; changing the variable needs a restart.
- Hosted vendor adapters (`openai`, `anthropic`) are not built and each needs its own ADR.
