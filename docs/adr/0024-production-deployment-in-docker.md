# ADR 0024: Production deployment of the API in Docker

- Status: PROPOSED (2026-10-18). Reviewed six times by `security-reviewer` and `risk-reviewer`:
  no blockers in any round, and no Majors in rounds 5 and 6. Findings by reviewer:
  - `security-reviewer`: round 1, the owner role was a superuser reachable over the network
    (Major); round 2, ignoring `.env` was silent (Minor); round 3, a `.env` created late or
    listed second, and holes in the compose scan.
  - `risk-reviewer`: round 2, ignoring `.env` was fail-open, and `verify()` did not check schema
    or database ownership (Majors); round 3, membership of a superuser role and the right to set
    `session_replication_role` (two Majors); round 4, the same trigger bypass through a role or
    database *setting* (Major).
  - Rounds 5 and 6 (both): Minors only. Fixed: the role script is pinned line by line (a `--`
    line before the heredoc counts as shell, input must be printable ASCII, split on newline
    only), `CREATEROLE` is checked through membership, the build context may not re-include
    files, the Dockerfile is parsed (one stage, one `USER`, no group changes, both services
    built from it with no `target`), and `.gitattributes` keeps the scripts on LF endings. The
    rest is listed under "Not solved".
  The text after round 6 has not been seen by either reviewer.
- Builds on ADR 0012 (roles), 0015 (migrations), 0023 (model egress). Closes the "needs its own
  compose file and ADR" gap recorded in ADR 0023.
- **Started, not exercised, on the developer machine** (2026-10-18, four runs and a restart,
  throwaway secrets, a placeholder model key; no assessment was created and the model host was
  never contacted). That shows the services start and the checks pass; it is not a tested
  deployment: see "Run for real", "Not solved" and "Conditions before acceptance".

## Context

`docker/docker-compose.yml` is a development stack. It gives the API the owner-role database URL,
passes the whole `.env` through `env_file` (so the model key, the MET token and the key hashes sit
in the container environment, visible in `docker inspect` and `/proc/<pid>/environ`), and has no
`sslmode`, so `environment=production` would refuse to start on it. The owner has decided
(2026-10-18) that production cannot run with the agent stage off and that the model host is
`https://wall-e.ch30ng.org`. The owner chose: PostgreSQL in a container of the same compose file,
the API only (no web UI yet), secrets as files, and TLS terminated by a reverse proxy the operator
supplies.

## Decision

`docker/docker-compose.production.yml` runs four services. Nothing in it is a secret.

1. **`db-tls`** (one-off, no network): creates a self-signed certificate for the database in a
   volume, and renews it when it has under 30 days left (when the stack is next brought up; a
   running database keeps the old certificate until it restarts). Nothing is kept in the
   repository. It runs as root with `CHOWN`, `DAC_OVERRIDE` and `FOWNER` added back.
2. **`db`** (`pgvector/pgvector:pg16`): TLS on, and `docker/db/pg_hba.conf` is loaded explicitly.
   Three roles exist:
   - `divesafe_bootstrap`: the image makes `POSTGRES_USER` a **PostgreSQL superuser**. It is
     reachable only through the local socket inside the container; no network rule names it. It is
     used by the entrypoint and the health check.
   - `divesafe_owner`: **not a superuser.** Owns the `divesafe` database and creates the schema
     for `db-init`. Created by `docker/db/10-create-app-role.sh` on the first start of an empty
     volume.
   - `divesafe_app`: what the API uses. No superuser, create, replication or row-security-bypass
     right, no create right in `public`, and no privilege on any table until `db_init` grants it.
   Over the network only `divesafe_owner` and `divesafe_app` may connect, only to `divesafe`, only
   with TLS and a SCRAM password, and only from the same subnet (the `data` network; on Linux the host can probably reach the
   database through that bridge too, which was not checked). A plaintext connection matches no
   rule and is refused. The script also removes the default `CONNECT` right of `PUBLIC` on the
   `postgres` and `template1` databases (`PUBLIC` keeps its other default rights, and superusers
   are not affected). Passwords are passed to psql through the environment and `\getenv`, not on a
   command line, and `log_min_error_statement` is set to `panic` for the statements that carry
   them, so a failure does not write them to the server log. Port 5432 is not published to the host.
3. **`db-init`** (one-off): runs `python -m divesafe.services.db_init` as `divesafe_owner`. It is
   the only service given the owner URL. It applies the migrations and grants the API role
   SELECT on `schema_migrations`; SELECT, INSERT and UPDATE on `assessments` and
   `run_failure_counts`; and SELECT, INSERT, UPDATE and DELETE on `pending_runs` (ADR 0012, 0015).
   The API waits for it to finish.
4. **`api`**: `DIVESAFE_ENVIRONMENT=production`, `DIVESAFE_LLM_PROVIDER=openai_compatible`,
   `DIVESAFE_ALLOW_EXTERNAL_LLM=true` (stated in the file, because every assessment sends data to
   the model host; see "What leaves the machine"), no `DIVESAFE_LLM_SERVER_IS_LOCAL`. The model
   URL, allowed host, model name and evidence age have **no default** (`${VAR:?...}`): a forgotten
   setting stops the stack instead of falling back to a value nobody chose.
   `DIVESAFE_EVIDENCE_MAX_AGE_MINUTES` has no built-in default in the application either; without
   it no assessment can be created (HTTP 503).

**Networks.** `data` is `internal: true`, so the database and the schema job have no route out.
The API is on `data` and on `egress`, which has outbound access: it needs the model host and the
data sources. Nothing restricts which hosts the API can reach beyond the application's own
allow-lists (see "Not solved").

**Hardening.** Every service drops all capabilities (`db` and `db-tls` add back only those their
entrypoints need) and sets `no-new-privileges`. The API and the schema job have a read-only root
filesystem and do not run as root. The API has a `/tmp` tmpfs limited to 16 MiB with `noexec`,
`nosuid` and `nodev`, a 1 GiB memory limit and a 256-process limit, its port bound to
`127.0.0.1`, and a health check on `/health`. The Docker build context excludes
`docker/secrets`, `docker/production.env`, and `*.pem` and `*.key` at any depth.

**Secrets as files (new code, `config/secret_files.py`).** When `DIVESAFE_SECRETS_DIR` is set,
`Settings` also takes five values from files in that directory: `divesafe_database_url`,
`divesafe_admin_database_url`, `divesafe_llm_api_key`, `divesafe_met_api_token` and
`divesafe_api_key_hashes`. Rules, each with a failing test:

- Only those five names are read. A file named like any other setting (`environment`,
  `allow_external_llm`, ...) is ignored, so a stray file cannot flip a safety setting.
- A value present both as a file and as a non-empty environment variable is **refused**, not
  resolved by precedence. The message names the setting, never a value.
- With `DIVESAFE_SECRETS_DIR` set, the presence of a `.env` file (the one `Settings` is configured
  to read) is an **error**: start-up is refused and the message names the file, never a value. A
  value in it would otherwise lose silently to a file, and simply ignoring the file would drop
  production settings without notice and fall back to development defaults. Without a secrets
  directory, `.env` works as before. (Found by the security review; the first fix ignored the file,
  which both reviewers then flagged as fail-open.)
- `DIVESAFE_SECRETS_DIR` itself is read from the process environment only, never from `.env`. A
  missing directory is an error, so a mistyped path cannot start the server with no secrets.
- An empty or blank file means "not set", as an empty variable does. A file is refused when it is
  over 64 KiB, not UTF-8, contains NUL, is a dangling symlink, or is not a regular file (checked
  on the opened descriptor, so a named pipe cannot hang start-up; a symlink to a regular file is
  followed). Except for the key hash table, a value must be one line of printable text, which
  also rejects vertical tab, form feed, NEL and the Unicode line and paragraph separators.
- Every other validation still runs on the value (the production `sslmode` check, the MET token
  shape, the key-hash table), and no error or `repr` contains it.
- `get_settings()` and `db_init` both use the loader. Unset, behaviour is exactly `Settings()`.
- **The running application refuses to start in production if `DIVESAFE_ADMIN_DATABASE_URL` is
  set.** The owner URL belongs to `db_init` only; the compose file does not give it to the API,
  and this check keeps a later mistake from doing so. It applies to production only.

**Smaller code changes.** The start-up log line "agent stage is ON" now names the model host (host
name only: no port, path, credentials or query). A start-up warning is logged when PHN e-Tide data
is enabled and the egress basis is not `declared_local` (tested for a loopback and a remote
server), because the PHN item then goes to the model host with every assessment while PHN's terms
are not on file (ADR 0021). The PHN connector is off in this compose file and the test pins that
the API gets no setting that is not listed in the compose file (the Dockerfile sets no `DIVESAFE_*` setting, only two Python variables).

**Database URLs** made by `docker/make-secrets.sh` carry `sslmode=require&channel_binding=require`.
With SCRAM over TLS, channel binding ties the login to the server's own certificate, so a
man-in-the-middle that relays the exchange fails even though the self-signed certificate is not
verified. It was checked against the real database. Production settings still require only
`sslmode`; they do not insist on channel binding.

## What leaves the machine

Every production assessment sends the **plan fields, the evidence items, the deterministic rule
results and the specialist findings** (the prompt layer's payload) to
`wall-e.ch30ng.org`, by the owner's decision that the agent stage cannot be off.

- That host's operator is not known. It probably forwards prompts to a hosted model (inferred
  from header names in the live runs, not confirmed), so the data may pass through a third party
  (ADR 0023).
- The evidence comes from Open-Meteo (licensed for non-commercial use), data.gov.my and, when the
  `divesafe_met_api_token` file is non-empty, MET Malaysia. **Those sources' terms on passing their
  data to a third-party model host were not checked** (ADR 0023).
- PHN e-Tide data is **off** in this stack. If it is turned on, its terms are not on file and the
  start-up warning above says so, but nothing stops it (ADR 0021).
- The host and the egress basis are in the start-up log. **They are not recorded on the assessment
  record**, so a stored assessment does not say where its data went.
- If the model host cannot be reached (DNS, trust store, certificate expiry on 2026-12-20, a path
  change), the agent stage fails for that assessment, is recorded as an agent issue, and the
  deterministic result stands. That is safe, but production would then be running without agent
  output while appearing configured for it.

## How to run it

Started on a developer machine, not exercised through an assessment (see "Run for real").

```
docker/make-secrets.sh docker/secrets            # creates the database secrets, never overwrites
# add by hand: docker/secrets/divesafe_llm_api_key and docker/secrets/divesafe_api_key_hashes
cp docker/production.env.example docker/production.env   # fill in the blanks
docker compose -f docker/docker-compose.production.yml --env-file docker/production.env up --build
```

`docker/secrets/` and `docker/production.env` are git-ignored.

## Tests

- `tests/safety/test_secret_files.py` (44): every secret-file rule above, the exact allow-list, the
  `.env` refusal (and that `.env` still works without a secrets directory), a dangling link, a named pipe, the line separators, production TLS on a
  file-supplied URL, `db_init` reading its file, and the owner-URL refusal in production only.
- `tests/safety/test_production_compose.py` (34): a text scan of the compose file and the files it
  mounts, with comments stripped first. It fails on YAML anchors, merges, `extends`, `include`,
  `privileged`, `pid` and host networking, and then pins: no `env_file`; no secret-named
  environment value; the API never given the owner URL, and only `db-init` holding it; the exact
  list of settings the API gets; production with the agent stage on and the opt-in stated; no
  default for the four required settings; the top-level secret-to-file map; the exact networks of
  each service; no published or exposed database port in any form; the exact capabilities; no
  Docker socket, no volumes for the API and the schema job, and the unprivileged `USER` line in
  the Dockerfile (neither service sets `user:`); resource limits and a
  `noexec` `/tmp`; `ssl=on` and the `hba_file` argument on the database; `pg_hba.conf` naming only
  the two roles, to one database, never `all`, never the bootstrap role; the role script making no
  superuser and no role grant, keeping passwords off the command line and the log; the secrets
  script never overwriting or printing a generated password; and the git and Docker ignores.
  The service set, the `db` command list, the `db-init` command and every non-comment line of the
  role script (in order, so a swapped password or a line after the terminator fails) are pinned
  exactly. A text scan reads the layout of this file; it does not understand YAML, and it does
  not cover every way a file could be written: `make-secrets.sh` is checked for a pipe into
  `write_new`, so a pipe through `tee` on that line is not detected. The Dockerfile is scanned
  only for its `USER` and `useradd` lines.
- `tests/safety/test_llm_egress_declaration.py`: the host in the log and nothing else of the URL;
  the PHN warning for a loopback server, a remote server and a declared-local one.
- `tests/integration/test_postgres_guard.py` (needs Postgres, so not part of
  `pytest -m safety` without it; CI (PostgreSQL 16) will run them once this is committed; 71 passed against a local
  PostgreSQL 17):
  `verify(least_privilege=True)` now also reports an application role that owns the `public`
  schema or the database; can create objects in `public` or schemas in the database; is, or is a
  member of, a superuser role (it could `SET ROLE` to it; the superuser attribute itself is not
  inherited); is or is in a role with `CREATEROLE`; is a member of `pg_execute_server_program`, `pg_write_server_files` or
  `pg_read_server_files`; has `session_replication_role = replica` set for the
  role, the database or the server (reported even without the least-privilege check, because
  ordinary triggers do not fire in that mode); or (PostgreSQL 15 and later) may
  `SET session_replication_role`. A
  schema or database owner can drop the append-only table, and no trigger fires for that. On
  PostgreSQL 14 and earlier `PUBLIC` can create in `public`, so the check fails every role there:
  PostgreSQL 15 or later is required (the image is `pg16`). `verify()` runs once, at API
  start-up: a grant or setting changed later is not seen until the next start, although a changed
  setting takes effect at once, because every operation opens a new connection (changing it
  needs a superuser, or a role granted `SET` on that parameter). The real `divesafe_app` role
  passed the checks in the fourth stack run, which had the replica-mode setting, `pg_read_server_files`
  and the role-only `CREATEROLE` check. The `CREATEROLE`-through-membership form was added after that
  run and has not run against the real stack (the real role has no memberships). The failure
  cases are tested only against a test role on PostgreSQL 17, not on the real image.
- **Mutation checks: 133 in seven rounds.** Round 1 (35: the loader, the host log, the PHN warning and
  the compose files): all caught. After the reviews, round 2 (34: the review fixes, new compose
  and role-script edits): 32 caught and 2 survived, because the first versions of the tests
  never switched `.env` reading back on and never checked that the owner-URL refusal is limited to
  production. Both tests were fixed and both mutations are now caught. Round 3 (6: the `.env`
  refusal, the three new `verify()` checks, a short read of a secret file): all caught. Round 4 (18: superuser membership, the file and
  program roles, database `CREATE`, the replication-role right and its version gate, `.env`
  handling for a late file, a tilde and a list, role-script and `pg_hba.conf` edits, and
  compose isolation keys): all caught. Round 5 (16: the replica-mode setting check and its
  independence from the least-privilege flag, the `CREATEROLE` and `pg_read_server_files` checks,
  an added service, an extra database setting, a changed `db-init` command, `root:root` and a root
  group for the API, extra statements in the role script, a nested `.env` in `.dockerignore`): all
  caught. Round 6 (11: `CREATEROLE` through membership, a swapped password, the wrong password
  file, a `psql` call before and a line after the heredoc, a replaced, reordered statement, a
  re-included `src/.env`, a removed or added `USER`, a `user:` in the compose file): all caught.
  Round 7 (13: a `--` psql option at column 0, a vertical tab and a non-ASCII byte in the role
  script, a trailing space after the terminator, a lower-case, indented or second `USER`, a second
  stage, `usermod`, another Dockerfile or a `target` in the compose file, an indented `!` line in
  `.dockerignore`): all caught. The PostgreSQL 14 branch of the version gate cannot run on 16
  or 17; only an inverted gate was mutated.
- **Run for real, on the developer machine, with the local pgvector image:** `db` and `db-tls`
  start and the database becomes healthy. Against a throwaway set of secrets, from a container on
  the `data` network: a plaintext connection is refused for both network roles; the bootstrap
  superuser is refused over the network; a TLS connection works and reports SSL; neither the owner
  nor the app role has a dangerous attribute; the owner can create a table and owns the database;
  the app role cannot create a table in `public`; a wrong password, `template1` for the app role,
  and the `postgres` database for the owner are refused; a connection with
  `channel_binding=require` works; and the server log held neither password. Everything was removed
  afterwards, including the throwaway secret files.
- **Whole stack, first run** (image `divesafe-api:local`, built from the unchanged Dockerfile; secrets
  and a placeholder model key made for the run; the model host was not contacted and no assessment
  was created). It was run a second time after the review fixes (rebuilt image), then stopped
  (`down`, volumes kept) and brought up again on the same volume: the database skipped
  initialisation, `db-init` reported "schema is up to date", and the API was healthy:
  - `db-tls`, `db`, `db-init` and `api` came up; `db-init` ran as `divesafe_owner` and applied
    migrations 1 and 2; the API reached `healthy` and started past
    `verify(least_privilege=True)` with `storage: durable`.
  - Grants for `divesafe_app`: SELECT, INSERT, UPDATE on `assessments` and `run_failure_counts`;
    SELECT, INSERT, UPDATE, DELETE on `pending_runs`; SELECT on `schema_migrations`. All four
    tables are owned by `divesafe_owner`.
  - The API container runs as uid 10001, sees four secret files and **no** owner file, and its
    environment (and `docker inspect`) hold no secret; the only match for a `KEY` search was
    `GPG_KEY`, the Python base image's public signing key id. The root file system is read-only,
    `/tmp` is writable but a copied binary cannot be executed there, and the memory, process and
    capability limits are applied.
  - Without credentials a `POST /v1/assessments` returned 401, and so did one carrying only an
    `X-Dev-Actor` header. The start-up log named `wall-e.ch30ng.org` with basis
    `remote_external_opt_in`, and did not warn about a missing decision age or about PHN.
  - **The API saw the Docker network gateway (`172.18.0.1`) as the peer of a request made from the
    host.** A reverse proxy on the host will therefore appear as that address, not as the
    proxy's own. Listing the gateway in `DIVESAFE_TRUSTED_PROXIES` makes the API believe
    `X-Forwarded-For` from **every process on the host that can reach port 8000**, not only the
    proxy (see "Not solved"). The gateway subnet is assigned by Docker; fixing it in the compose
    file is not done.

## Conditions before acceptance

The status stays PROPOSED until all of these are done.

1. **Open:** the stack has been started, not exercised. Needed: an assessment created through the
   API on this stack's roles (reaching the model host, with the real secrets on the real host),
   seen as `PENDING_HUMAN`, and decided through the API (write-once); then a restart with that
   data in the volume. What has been done: images built, the stack started four times, a restart
   on the same volume, and 401 without credentials (see "Run for real").
2. ~~The owner chooses `DIVESAFE_DECISION_MAX_AGE_MINUTES`.~~ Done: 180 minutes (owner,
   2026-10-18), the compose default, pinned by a test. In this compose file an empty or unset
   variable becomes 180 (`:-`), and `0` or a negative number is refused by the settings, so this
   stack cannot run without a limit. Outside this file, an unset variable still means no limit
   with a start-up warning; the application does not refuse that in production. Nothing records
   why 180 was chosen beyond the owner's decision.
3. A backup and restore procedure exists. `db-data` holds the only copy of the append-only record
   of human decisions.
4. A value for `DIVESAFE_EVIDENCE_MAX_AGE_MINUTES` is recorded with its reason before any ruleset
   is mounted. The setting accepts any positive integer. With no ruleset it changes nothing (every
   assessment is INSUFFICIENT EVIDENCE); once signed limits exist, it decides whether old evidence
   can support GO or CAUTION, and it is a safety limit that no one has reviewed.

## Not solved

- **The secrets reach the container as files, but still pass through the host.** They sit in
  `docker/secrets/` with mode 0444 inside a 0700 directory; anyone who can read that directory or
  the Docker socket can read them. A secrets manager is not used. The API runs as a different user
  (uid 10001): the 0444 mode is what lets it read them, so a file tightened to 0400 makes the API
  fail to start rather than run without it.
- **The database certificate is self-signed and the API does not verify it.** `sslmode=require`
  (the weakest mode the production settings accept) encrypts the connection but does not check who
  answers; channel binding narrows the gap, it does not close it. `verify-full` needs a CA file
  mounted into the API; not built.
- **No outbound restriction.** The API can reach any host the application's allow-lists permit.
- **TLS in front of the API is the operator's.** Port 8000 is plain http on `127.0.0.1`. The proxy
  must overwrite `X-Forwarded-For`. The API sees the Docker gateway address as the peer (observed),
  so that address, not the proxy's, would have to go in `DIVESAFE_TRUSTED_PROXIES`. **That trusts
  every local process that can connect to `127.0.0.1:8000`**: it can send its own
  `X-Forwarded-For` and choose its failed-login bucket, which weakens the brute-force limit
  (it does not bypass authentication). Leaving the list empty is safe but makes everyone behind
  the proxy share one bucket, so one client's failures can lock out the others. Neither is solved
  here; a proxy on a fixed address of its own network would fix it. The start-up warning checks
  only that the list is empty.
- **No web UI, log shipping or monitoring.** (Backups are a condition above.)
- **A signed ruleset is not mounted**: `DIVESAFE_RULESET_PATH` is unset, so every assessment is
  INSUFFICIENT EVIDENCE until a dive professional's limits are signed and mounted.
- **The app role's password is set once.** Changing a password secret later does not change the
  role; run `ALTER ROLE` as the owner. The roles are created only on an empty volume.
- **An old volume keeps its old roles.** The role script runs only on an empty volume. A volume
  made by the first version of this stack would keep a superuser `divesafe_owner`, reachable over
  the network by `pg_hba.conf`; nothing reports it (`verify()` runs as the app role, and
  `migrate` does not refuse a superuser, because the development stack uses one). The first
  version was only ever run on throwaway volumes. Recreate the volume or check `rolsuper`.
  A check that the *owner* role is not a superuser is not built. It would be one column in the
  production-only part of `verify()` (the app role can read the catalogs), and it would not touch
  the development stack. It is left out because it is a policy choice for the owner: it would
  refuse any production deployment, Docker or not, whose owner role is a superuser.
- **The guard triggers are checked for existence, state and function name only.** `verify()` does
  not compare their timing or function bodies, and does not report another trigger or rule on
  `assessments`. A later migration (reviewed, owner-run) could add a BEFORE trigger that runs
  after the guard; the API reads each row back and rolls back on a mismatch, so it fails closed on
  that path. Making the triggers `ENABLE ALWAYS` would close the replica-mode class for good; it
  needs a reviewed migration, changes the `tgenabled` checks, and must also change the word scan
  in `migrations.py` that refuses any later statement naming a guard trigger. It is not done.
- **A setting changed after start-up is not seen.** `verify()` runs once, but every operation
  opens a new connection, so a superuser's `ALTER ROLE ... SET session_replication_role = replica`
  (or a reload after `ALTER SYSTEM`) takes effect at once and unseen. Write-once is still enforced
  in code, so only the database backstop is lost. Re-reading the setting inside the write
  transaction would close it; not built.
- **`make-secrets.sh` is scanned only for a pipe into `write_new`.** A pipe through `tee` on that
  line is not detected.
- **A failed first start leaves a half-set-up volume** (recalled, not checked). If the role script
  fails, the image keeps the initialised data directory and skips the script on the next start,
  so some statements may be missing. Remove the volume and start again.
- **`verify()` is not the guard.** It is a start-up check; the guard is the triggers and the
  grants.
- **Upgrading PostgreSQL.** The image is pinned to major version 16. A volume is not compatible
  across major versions; moving to another major version needs a dump and restore, which is part
  of the missing backup procedure.
- **Port binding on older engines.** A port published to `127.0.0.1` was reachable from the local
  network on some older Docker Engine versions (recalled, not checked). State a minimum version
  once one is chosen.
- **Base images float.** `python:3.12-slim` and `pgvector/pgvector:pg16` are tags, not digests.
  The Dockerfile installs from PyPI without a lock file.
- **Not run:** an assessment through the API (needs credentials and the model host), the 30-day certificate
  renewal, and the stack on Linux (the run was on Docker Desktop for macOS, where file ownership
  and port publishing differ). On Linux the host reaches the published port through the bridge
  gateway too; what the API sees as the peer there was not observed.
- **Whether the postgres entrypoint leaves a password in a process environment** after reading it
  from a file was not checked.
- **`db_init` uses `print`** (AGENTS.md forbids it in `src`). It prints no secret.
- **The `psycopg-3.3.6-py3-none-any.whl` file in the repository root** (committed on 2026-10-10)
  is unrelated to this ADR and not used by any file here. Its purpose is unknown to this ADR.
