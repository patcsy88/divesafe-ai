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
- **Amendment 1 (2026-10-21), the knowledge loader,** is at the end of "Conditions before
  acceptance", as ADR 0022 requires. Status stays PROPOSED; open conditions unchanged. **Nothing in
  the amendment is built or run**, and neither reviewer has seen its revised text (revised the same
  day after both reviews; see the note at its start).
- Builds on ADR 0012 (roles), 0015 (migrations), 0023 (model egress). Closes the "needs its own
  compose file and ADR" gap recorded in ADR 0023.
- **Started on the developer machine** (2026-10-18, four runs and a restart, throwaway secrets, a
  placeholder model key; no assessment was created and the model host was never contacted), and
  **one assessment was created and decided on the owner's host with real secrets on 2026-10-19**
  (partly seen: see condition 1). That shows the services start, the checks pass and one
  assessment can go through; it is not a tested deployment: see "Run for real", "Not solved" and
  "Conditions before acceptance".

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
  `allow_external_llm`, ...) is ignored, so a stray file cannot flip a safety setting. (Amendment 1
  adds a sixth name, the loader's; until that is built the list and its pin stay five.)
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

Started on a developer machine; one assessment went through on the owner's host (see condition 1
and "Run for real"). The step-by-step procedure is `docs/first-run-on-the-real-host.md`.

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
- `tests/safety/test_production_compose.py` (37; the secret-name scan exempts exactly one key,
  `DIVESAFE_LLM_MAX_OUTPUT_TOKENS`, on the `api` service with a 3 or 4 digit default, because its
  name contains TOKEN but it is a model output budget; a separate test feeds the scan ten made-up
  bad environments and requires each to fail; the token and time budgets are pinned together):
  a text scan of the compose file and the files it
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
- `tests/safety/test_backup_scripts.py` (59), in three layers. (1) Every non-comment line of
  `backup.sh`, `restore.sh` and `backup-common.sh` is compared with a reviewed copy in
  `tests/safety/golden_backup/`, so any edit fails until someone reads the diff and updates the
  copy: this is a change detector, not a proof that a change is wrong. (2) Scans that name the
  intent: no password on a command line, each password read once inside the database container;
  no `--clean`, `--create`, trigger switch-off, `TRUNCATE`, `DROP`, `ALTER` or `CREATE` of
  functions, triggers or roles; no script uses the database superuser (the dump, the fact queries
  and the real load all run as `divesafe_owner`); the fact queries pin the search path and cast
  names to text; the archive is never loaded by the production superuser and the real load is
  the one `pg_restore` as `divesafe_owner`; the scratch container has no network, no secrets, no
  mount and no TCP listener; its image is the one in the compose file; every refusal comes
  before the load; archives, `.part` files and manifests are git- and Docker-ignored. (3) The
  real scripts run against a fake `docker` on `PATH`: the backup keeps nothing when the scratch
  facts differ, a guard trigger is missing, the facts cannot be read, the directory is open or
  inside the repository, the lock is held or the database keeps changing; the restore refuses and
  never reaches `pg_restore` for a running API, a used database, a changed archive, a missing or
  wrong-format manifest, a manifest without the guards or a scratch result that differs, and
  reports a mismatch after the load. The fake cannot run SQL, so the content of the facts, the
  scratch server and the real restore are covered only by the scans and by the hand run
  (condition 3).
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
- **Mutation checks: 133 in seven rounds, then 43, 32, 20 and 7 on the backup scripts (below).** Round 1 (35: the loader, the host log, the PHN warning and
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
- **Mutation checks on the backup scripts (after both reviewers' first round):** 38 edits to the three
  scripts and the two ignore files, then 5 more: all caught by the full test file. The first
  version of the test had let 3 survive (a commented-out test restore, a dump written straight to
  its final name, a bad pattern). With the line-by-line comparison switched off, 7 of the 38 survived
  (`service=apix`, restoring from the original file instead of the private copy, `set +e`,
  skipping the stable-facts check, dropping the `guard_defs` fact, dropping the `C` collation, and
  counting guard triggers instead of naming them). The fake-docker and scan tests were then
  extended and the 7 are caught without the comparison too, but the last three are caught by text
  scans of the SQL, not by running it.
- **Second round on the backup scripts.** Both reviewers' second round found that the fact
  queries ran as the database superuser: a view or an aggregate that the owner or an archive puts
  in `public` then runs with superuser rights, so "the superuser only reads" was wrong. The
  scripts now use no superuser. A hand run planted such an aggregate as the owner: the backup did
  not fire it, and the same query run unpinned as the superuser did (the control), so the attack
  was real. 32 edits to the new logic were then mutation-checked: all caught by the full test file.
  With the line-by-line comparison off, 6 survived: a `name` column not cast to text, a wider
  scratch tmpfs, a failed live read and a failed scratch read that had already printed every
  fact, an ACL case that was not refused, and `before="x"`. Tests were added and the first five
  are caught without the comparison too. `before="x"` is an equivalent mutant: the second read
  would have to equal it, so it is caught only by the comparison and changes no behaviour. My
  first version of the new failure test was red on the unmodified scripts (a `&& exit` that
  returned non-zero), so its first "caught" results were void; they were rerun after the fix.
- **Third round on the backup scripts (both reviewers, no blockers).** Fixes: the check for a second
  database now counts every container labelled as a compose `db` service (the image filter could miss
  an old container); a `hardening` fact that must be 0 (policies, row security switched on,
  default privileges, stored role settings, rules, a superuser owner) and `row_security = off` for the
  fact queries; the ACL check counts `ls -lde` entries; the restore prints the time of the latest
  assessment and keeps the unknown-runs list in a stripped, pinned report; the reconcile steps in
  the document no longer rely on `created_utc`. 20 edits were mutation-checked: all caught by the
  full file. My first run of them was void (the suite was red on the unmodified scripts: my new
  message contained a word the scan forbids) and was repeated. Without the line comparison 2
  survived (`pg_db_role_setting` dropped from `hardening`, the latest-assessment line); tests were
  added, and both are caught without the comparison too.
- **Fourth round on the backup scripts (both reviewers: no blockers, no new Majors).** Small fixes:
  `hardening` also counts schemas other than `public`; the macOS directory check requires
  `ls -lde` to succeed and print one line; the restore report is filtered under `LC_ALL=C`; the
  reconcile step names every decider-role key, the limit being raised later, and the host clock;
  "never writes over data" is qualified to archives made by `backup.sh`. 7 edits were
  mutation-checked: all caught by the full file; 1 survived without the line comparison (the
  `pg_` exclusion of the schema term), and a test now catches it. A hand run showed a schema
  planted by the owner refused with nothing written, a directory with an ACL refused, a directory
  with only an extended attribute accepted, and a restore into a new volume. Linux, where only the
  `+` in `ls -l` is checked, was not run. Not done from the reviews: the test rename, a pinned
  guard hash, naming the role that saw update/delete/truncate refused, and checking the owner role
  before the load on restore (it is checked after, and the script then says not to start the API).
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

1. **Mostly done (repeat run below); still open: a Linux host and per-call
   durations.** Needed: an assessment created through the
   API on this stack's roles (reaching the model host, with the real secrets on the real host),
   seen as `PENDING_HUMAN`, and decided through the API (write-once); then a restart with that
   data in the volume. What has been done: images built, the stack started four times, a restart
   on the same volume, and 401 without credentials (see "Run for real").
   **First real run (owner, macOS host, 2026-10-19, `docs/first-run-on-the-real-host.md`), seen
   from the final record the owner pasted:** one assessment for `my-pahang-pulau-tioman` was
   created, the model host answered (a risk proposal and four token-limit stops are on the record;
   `model_version` names the **configured** model `claude-sonnet-5-5`, and which model actually
   answered depends on the host's routing and is not recorded), the recommendation was
   `INSUFFICIENT EVIDENCE` (no tide evidence; every limit TBD; the explanation agreed and tightened
   nothing), and the owner's decision `NO-GO` was recorded (`is_override` true, status `DECIDED`,
   not a less severe override). Open-Meteo marine and wind and the two data.gov.my sources
   returned; MET was not used (empty token file). **Not done or not seen:** the agent stage lost
   four calls to the token limit (`specialist:weather` and the three scenarios), with the
   compose file passing no `DIVESAFE_LLM_MAX_OUTPUT_TOKENS`, so the 1500 default applied: the
   compose file now defaults it to 3000 together with a 120 s call and a 300 s stage timeout (the
   three values evaluation runs 2 and 3 used; run 1 used 1500/60/150; the run deadline derived from them is 360 s), and
   **those settings were run in the repeat below**. A budget of
   3000 can still stop a call at the limit (one did in evaluation run 2), and the 1 MiB response
   limit counts the raw stream, so a long reply is refused, never trimmed. `model_version` does
   not record the token budget, so a record made at 1500 cannot be told from one made at 3000.
   That first paste did not show `/health`, the 401, the 409 or the record after a restart; the
   repeat run covers all of them. `model_version` does not record the host or the egress
   basis. An earlier
   attempt the same day was abandoned and its secrets deleted; no stack, volume or assessment
   existed afterwards, and whether it sent anything to the model host is not known (its
   model-host key file was missing).
   **Repeat run (owner, macOS arm64, Docker Compose, 2026-10-20, empty volume, compose budgets
   3000/120/300), seen from the output the owner pasted:** `/health` returned
   `{"status":"ok","version":"0.1.0","storage":"durable"}`; `/v1/sites` without credentials returned 401. The create returned 202 with a
   `Location` header and status `RUNNING`; two polls were `RUNNING`; the finished record was
   there about 46 s after the POST (inside the 360 s deadline), `PENDING_HUMAN`, with
   `agent_issues: []`, so none of the four calls hit the token limit this time. Recommendation
   `INSUFFICIENT EVIDENCE` (deterministic, proposed and final equal; no tide evidence;
   `llm_tightened` and `llm_attempted_downgrade` false); five findings, three scenarios and an
   explanation were produced; `model_version` was
   `openai_compatible/claude-sonnet-5-5; prompts=2026-10-17.2`. MET was not used. The owner's
   `NO-GO` (actor `first-run`) was recorded, `DECIDED`, `is_override` true, not a less severe
   override; a second, different decision (`CAUTION`) returned 409. After `restart api` and after
   `down` (no `-v`) then `up -d --wait api` (db healthy at 8.0 s, api at 12.9 s) the record was
   identical and `DECIDED`. **Not seen:** individual call durations, a Linux host,
   and any other data set; this is one record from one host.
2. ~~The owner chooses `DIVESAFE_DECISION_MAX_AGE_MINUTES`.~~ Done: 180 minutes (owner,
   2026-10-18), the compose default, pinned by a test. In this compose file an empty or unset
   variable becomes 180 (`:-`), and `0` or a negative number is refused by the settings, so this
   stack cannot run without a limit. Outside this file, an unset variable still means no limit
   with a start-up warning; the application does not refuse that in production. Nothing records
   why 180 was chosen beyond the owner's decision.
3. **Partly done:** a backup and restore procedure exists (`docs/backup-and-restore.md`,
   `docker/backup.sh`, `docker/restore.sh`). `db-data` holds the only copy of the append-only record
   of human decisions. Run by hand on a developer machine (Docker Desktop, macOS) with synthetic
   rows (3 assessments, 2 decided, one an override, a microsecond timestamp, non-ASCII text):
   the backup read its facts from the live database and the archive, loaded in a throwaway
   container, reproduced them; `--check` matched while the API was up; a restore into a new empty
   volume, as `divesafe_owner`, matched the manifest; the API then started on it ("schema is up to
   date", `storage: durable`); update, delete and truncate on `assessments` were refused; the app
   role's grants were as before; a second backup had the same digest. The refusals were seen for a
   running API, a used database, a changed archive, a wrong directory mode, a held lock, and for a
   manifest whose `digest`, `guard_defs` or `assessments` line was altered (on an empty database,
   which stayed empty). Hostile objects were planted as the owner (not as the superuser) and the marker file was
   checked in the database container: a function in a CHECK constraint, an aggregate and a view over a
   function, each of which runs a shell command only for a superuser session. None fired during the
   backup or the restore, and each object was restored. **That shows only that no superuser session
   was used.** It does not show that the pinned search path works, because those objects act only for a
   superuser. A further probe did: an always-on `string_agg(name, text)` returning a fixed string was
   planted in `public` as the owner; run unpinned with uncast names it returned the fixed string
   (the control), and the backup still read the true trigger names and its scratch restore matched.
   Pinning and casting were not separated in that run; the mutation rounds do that. Two earlier
   probe runs were invalid and were repeated (the first planted the row as the superuser; one "two
   databases" test used a container without the compose label and so tested nothing). A third round
   of hand runs showed the refusals for a directory with both an extended attribute and an ACL, for
   a default privilege and for a row-level-security policy planted on the live database (nothing
   was written), a restore into a new volume that printed the time of the latest assessment, and the
   refusal of a second restore. The
   scratch container is removed when it ends, so what happened inside it was not seen. The refusal
   for two running databases was seen once the second container carried the label. Nothing here
   exercised a trusted extension, `ALTER DATABASE ... SET`, or a hostile archive made outside the
   database. The reconcile steps in the document come from reading the code, not from a run, and the
   list of unknown runs was empty, so its listing has run only against a fake `docker`. **Not done:** a run on Linux, with real data, with actual conditions or pending
   runs in the data, or with an archive from another host; a schedule that runs the backup, a
   failure alert, retention, encryption and an off-machine copy (the scripts do none of these).
   The owner decided on 2026-10-19 that the backup is **daily**, so up to about a day of
   assessments and decisions may be lost on a restore, and more if a run fails unnoticed;
   nothing in the repository runs it yet; a check of objects other than the guard in a restored
   database. After a restore, assessments and decisions made since the backup are gone and the
   document says how to reconcile them; the scripts cannot.
4. A value for `DIVESAFE_EVIDENCE_MAX_AGE_MINUTES` is recorded with its reason before any ruleset
   is mounted. The setting accepts any positive integer. With no ruleset it changes nothing (every
   assessment is INSUFFICIENT EVIDENCE); once signed limits exist, it decides whether old evidence
   can support GO or CAUTION, and it is a safety limit that no one has reviewed.

## Amendment 1 (2026-10-21): the knowledge loader

ADR 0022 (ACCEPTED with conditions, 2026-10-21) requires this amendment before its feature ships.
**Nothing in it is built or run.** No file under `docker/`, no compose service, no test and no
setting described here exists; the text below says what must be made. ADR 0024 stays PROPOSED and
its four conditions are unchanged. The feature does not ship until what follows is made and has been
reviewed by both `risk-reviewer` and `security-reviewer`. No decision above is changed and no pin
is weakened; every pin named here is added beside the existing ones. Names are fixed here where a
pin has to name them (A1, A4) and otherwise at code time.

**Revised after review (2026-10-21, same day, docs only).** `security-reviewer` and `risk-reviewer`
each reviewed the first text of this amendment and found Majors; this revision answers them and
**neither reviewer has seen the revision**. What changed: the role script's delivery, login, order
and password handling are specified (A1); `kb_load` gets its own narrow settings model (A3, A4); a
loader role-name setting and an exact `db-init` environment pin are named (A1, A5); the "outside
the repository" check is stated to be impossible inside the container (A3); `withdraw` has its own
service and rules (A3); restoring an older correct archive can bring withdrawn text back, now a
restore step and a gap (A7, A8); old 13-fact archives are refused accurately through a manifest
format bump (A7); the TEMP reasoning is corrected (A7); and the stage order forbids running the
first-stage image on the owner's host (A6). **Owner decisions recorded (2026-10-21):** (1) the
loader password file stays mounted into `db` permanently (A1); (2) `withdraw` is exempt from the
global advisory lock, a deliberate departure from ADR 0022 decision 11 (A3; ADR 0022 is annotated
and `risk-reviewer` must look at it); (3) the corpus variable keeps `${VAR:?}` with a placeholder in
`production.env.example` (A3); (4) `kb_load` takes an explicit development flag (A3). The files
the reviewers named as unread (`restore.sh`, `backup.sh`, `backup-common.sh`,
`docs/backup-and-restore.md`, `docker/db/*`, `make-secrets.sh`, the compose file, the production
guards in `settings.py`, `tests/safety/test_production_compose.py`) were read for this revision;
claims about them come from those files. What I could not confirm from files is marked **to
verify**.

### A1. The loader role and its password

- **Names.** Role `divesafe_loader`. Setting `DIVESAFE_LOADER_DB_ROLE` (field `loader_db_role`),
  validated with the same pattern as `DIVESAFE_APP_DB_ROLE` (`^[a-z_][a-z0-9_]{0,62}$`, ADR 0022
  decision 11) and given as a literal in the compose file, as the app role is. It is given to
  `db-init`, which grants to the role and fails if it is missing. `kb_load` does not need it: its
  self-check uses `current_user`. Password file `db_loader_password`, connection string file
  `divesafe_loader_database_url`. `db_init` and the role script both refuse a loader name equal to
  `divesafe_bootstrap`, `divesafe_owner` or the app role.
- **What changes.** The loader is created by the bootstrap superuser through a **new script, safe
  to re-run on an existing volume**. `divesafe_owner` is NOCREATEROLE, and
  `docker/db/10-create-app-role.sh` runs only on an empty volume, so neither can do it. The owner's
  host already has a volume (the repeat run of condition 1 used an empty volume on 2026-10-20). The
  script is **not** mounted into `docker-entrypoint-initdb.d` (that runs on new volumes only).
- **How the script gets in: stdin, with `exec -T`.** Chosen over a second read-only bind mount of
  the script into `db`, which would add one more mount to pin on a service that holds three
  passwords. The SQL file lives in `docker/db/` (name fixed at code time) and is redirected into
  `psql` from the host.
- **The loader password file stays mounted into `db` permanently (owner decision, 2026-10-21).**
  `db` already holds the bootstrap, owner and app passwords, so this adds a fourth of the same
  kind, not a new class of exposure; anyone who can `exec` into `db` or read the mounted file can
  read it. **Alternative considered, not taken:** pass the loader password on the standard input of
  one `exec -T` run so the file is never mounted into `db`. It would keep one secret out of the
  container; it was not chosen because `db` holds the others and the in-container read below is
  simpler to pin.
- **Order (the only correct one).** (1) `docker/make-secrets.sh docker/secrets`; (2) `docker
  compose ... up -d --wait db`, which **recreates `db`** so it has the new secret in its `secrets:`
  list and the server re-reads `pg_hba.conf` at start (the file is already a bind mount; a changed
  rule takes effect only when the server restarts); (3) run the role script (below); (4) `up`
  (`db-init` then finds the role). After a restore into a new volume, the order is in A7.
- **Login.** `pg_hba.conf` has `local all all scram-sha-256`, so the bootstrap role needs its
  password even on the socket. It is read from `/run/secrets/db_bootstrap_password` **inside the
  container** by a `sh -c` wrapper, as `in_db_owner` does in `backup-common.sh`. Never through host
  argv: no `exec -e PGPASSWORD=$(cat ...)`, no `-e` carrying any secret. Shape of the command (exact
  text fixed at code time and pinned by a scan of the runbook):

  ```
  docker compose -f docker/docker-compose.production.yml --env-file docker/production.env \
    exec -T db sh -c 'PGPASSWORD="$(cat /run/secrets/db_bootstrap_password)" \
    exec psql -X -v ON_ERROR_STOP=1 -U divesafe_bootstrap -d divesafe \
    -v loader_role=divesafe_loader -f -' < docker/db/<loader-role-script>.sql
  ```

  The host argv holds the wrapper text, not the password. The role name is not a secret.
- **What the script does.** (a) `SET log_min_error_statement = panic`, as the existing script
  does. (b) Refuses (non-zero, nothing written) a name equal to `divesafe_bootstrap`,
  `divesafe_owner` or `divesafe_app`, or one that does not match the setting's pattern. (c) If the
  role exists, refuses when it has SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS, cannot
  log in, **has any role membership (as member or as granted role), or has a stored role setting**
  (`pg_db_role_setting`). It does not repair it. (d) If the role is absent, creates it with `LOGIN
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS` and the password. (e) `GRANT
  CONNECT` on `divesafe` (idempotent). It grants **no table privilege**: those come from `db_init`.
  **Running it twice is safe**: the second run finds the role, passes (c) and changes nothing. It
  sets the password only when it creates the role (the existing "set once" gap applies).
- **psql details.** psql variables are **not expanded inside `DO $$ ... $$`** (a dollar-quoted body
  is one string), so the script branches with `\gset` and `\if`, builds the `CREATE ROLE` with
  `SELECT format('CREATE ROLE %I ...', ...) \gexec`, and signals a refusal with a statement that
  raises an error inside the `\if` branch (`\quit` exits zero). **psql echoes a failing statement**
  (a `LINE n:` excerpt) and the server logs it unless suppressed, so the password must not be in
  statement text that psql sends. Design **to verify** on a throwaway volume (not run): the password
  is read **by the server** with `pg_read_file('/run/secrets/db_loader_password')` inside the
  `format(...)`, so the text psql sends names only a path and the generated statement has no
  position to quote. That is why the permanent mount matters. (**To verify** also: that the
  bootstrap superuser may read that path in the container, and that the file's trailing newline
  handling matches `make-secrets.sh`, which writes none.) If it does not work, the fallback is the
  existing script's `\getenv` pattern with its residual risk: a syntax error in a statement that
  carries the password could be quoted to the terminal. The ADR text at code time says which was
  built.
- **The password secret.** `make-secrets.sh` gains the same pattern for the loader password file
  and the connection string file (`sslmode=require&channel_binding=require`), written by
  `write_new`: never overwritten, never printed, `0444` inside the `0700` `docker/secrets` (the
  container user differs, as for the existing secrets; a `0400` file fails to start). On the
  owner's host it creates only the missing files. The new files are git- and Docker-ignored by the
  existing `docker/secrets` entries. The `db` service gets the loader **password** secret (a change
  to its `secrets:` list; A5 pins it); the **connection string** goes to the loader services alone.
  **Documented case:** as for the owner and app secrets, the connection string is written only in
  the step that creates the password. If the password file exists and the connection string file is
  missing, `make-secrets.sh` will not make it: the operator writes it by hand from the password file
  without printing it, or, if the role is not yet created, removes both loader files and runs the
  script again. If the role exists with an older password, the file and the role disagree (the "set
  once" gap) and the superuser must run `ALTER ROLE`.
- **Failure behaviour.** The script exits non-zero and writes nothing when it refuses. `db_init`
  **fails with a fixed message if the loader role is missing and never creates it** (ADR 0022
  decision 11); the stack then stops at `db-init`, so the API does not start until the role exists.
- **Pins.** (a) The script is read line by line, as `_ROLE_SCRIPT_LINES` does for the existing one
  (printable ASCII, newline split; psql meta-commands are lines). It fails on a swapped password
  file, an extra statement, a `SUPERUSER`/`CREATEROLE`/`CREATEDB` word in a `CREATE`, a table
  `GRANT`, a password in statement text (`PASSWORD :'...'`), an unconditional `CREATE ROLE`, or a
  missing name or attribute refusal. (b) `test_the_secrets_script_prints_no_secret_by_any_command`
  scans `\$\{?(bootstrap|owner|app)_password`; the pattern is extended to `loader`, and a test
  fails if `write_new` is replaced by a bare redirect. (c) A scan of the runbook: its command has
  no `exec -e`, no `$(cat` outside the single-quoted wrapper, and the same role name as
  `pg_hba.conf`. Mutations: drop the existence guard; drop the membership check; add `CREATEROLE`;
  put the password in statement text; print the password; each must fail.
- **Documented gap.** Nothing proves the role in a running volume is the one the script made
  (`db_init`'s grants and `kb_load`'s self-check, ADR 0022 decision 11, are the check). A changed
  password secret does not change the role. The scan of `make-secrets.sh` still does not see a pipe
  through `tee`.

### A2. The `pg_hba` entry

- **What changes.** `docker/db/pg_hba.conf` gains one line, same form as the others:
  `hostssl divesafe divesafe_loader samenet scram-sha-256`. The owner and app lines stay exactly as
  they are.
- **Failure behaviour.** A plaintext connection, another database, another source or a wrong
  password matches no rule or fails SCRAM and is refused, as today.
- **Pins.** The rule list is pinned **exactly**, in order: `local all all scram-sha-256`, then the
  owner, app and loader `hostssl divesafe <role> samenet scram-sha-256` lines: four rules and no
  more. (Today's test checks the fields of each `hostssl` rule but not the list, so an added
  `local` or `host` rule would pass.) The loader name in the file equals the compose literal for
  `DIVESAFE_LOADER_DB_ROLE`. `test_the_bootstrap_superuser_is_not_reachable_over_the_network` is
  widened to an exact set of three allowed names, not a pattern; the check of `samenet`, database
  `divesafe` and no IPv6 range stays. Mutation checks: `host` instead of `hostssl`; `all` as
  database or user; a CIDR instead of `samenet`; `trust`; a rule for the bootstrap role; an extra
  rule; each must fail.
- **Documented gap.** Unchanged from the existing text: the host may reach the database through the
  bridge on Linux (not checked); the certificate is self-signed and not verified.

### A3. `kb_load` as a one-off compose service

- **What changes.** `docker/docker-compose.production.yml` gains two services that `up` does not
  start, both under a **compose profile** (for example `knowledge`), so only `docker compose
  --profile knowledge run --rm kb_load ...` or `... kb_withdraw ...` starts them:
  - **`kb_load`** runs `load` and `approve`.
  - **`kb_withdraw`** runs `withdraw` only (see "`withdraw`" below). It has the same hardening and
    the loader secret, and **no `volumes` at all**.

  Both use the same image and Dockerfile as `api` and `db-init` (`divesafe-api:local`, same
  `build` block, **no `target`, no `user:`**) and `restart: "no"`. `kb_load` has:
  - **depends_on:** `db` with `condition: service_healthy`, and nothing else. It does not depend on
    `db-init` (a `run` of a service that depends on the one-off schema job might start that job
    again: **to verify**); the loader's own self-check and schema check refuse when the grants or
    tables are absent. No service depends on either loader service.
  - **Network:** `[data]` only. It has no route out.
  - **Secrets:** exactly one, the loader connection string, mounted as a file under the
    `/run/secrets` target for the new `SECRET_FILES` name (A4). No LLM key, MET token, key hashes,
    owner or app secret.
  - **Mounts:** exactly one, the corpus directory, **read-only**:
    `${DIVESAFE_CORPUS_DIR:?...}:<fixed target>:ro`, with no default. No other volume.
  - **No `ports`, no `expose`.** Read-only root filesystem, `cap_drop: [ALL]`,
    `no-new-privileges`, no `cap_add`, memory and process limits as `api` has.
  - **Environment:** `DIVESAFE_ENVIRONMENT: production` (ADR 0022 condition 1), `DIVESAFE_SECRETS_DIR:
    /run/secrets`, and the one corpus-target setting the code needs. It does not get
    `DIVESAFE_LLM_*`, `DIVESAFE_ALLOW_EXTERNAL_LLM`, an owner URL, a key hash or any setting for the
    approved hash or the development flag.
  - **Entrypoint:** exec form, one fixed list (`["python", "-m", "<kb_load module>"]`, module fixed
    at code time), **no `command` key**, no `sh -c`, no shell, no `${...}` in it. The subcommand and
    arguments come from `docker compose run ... kb_load load|approve ...` and reach the process as
    arguments, never through a shell.
  - **Its own settings model (security finding S1).** The shared `Settings` refuses to start in
    production with `llm_provider=fake` (`settings.py` line 173) and without API key hashes (line
    180), and the loader gets neither `DIVESAFE_LLM_*` nor hashes, on purpose. So `kb_load` **cannot
    use the shared `Settings`**: it has a narrow model of its own that reads only the environment
    name, the secrets directory, the loader URL (with the production `sslmode` check) and the
    corpus target. The API-only guards do not apply to it. Whether it also applies the `.env` and
    file/environment conflict rules of the secret-file loader is **to verify at code time**.
  - **The approved manifest hash** is an operator argument, `--approved-manifest-sha256 <hash>`,
    validated `^[0-9a-f]{64}$`. It is **never** read from a file under the corpus mount, from an
    environment variable, from a compose variable or from a secret, so whoever can change the
    corpus cannot also supply the hash that approves it. The compose file has no setting for it. It
    is **not a secret**: it is visible in shell history, in `docker ps` and in `docker inspect` of
    the run container while it exists. It approves nothing by itself; it is also in the private
    approval record.
  - **The explicit development flag (owner decision, 2026-10-21; ADR 0022 condition 1).** In
    production the hash is required for `load` and `approve`. It is skipped **only when the
    operator passes the explicit flag `--development-no-manifest-hash` (name fixed at code time)
    AND `DIVESAFE_ENVIRONMENT` is not `production`.** The flag alone in production is refused, and
    `docker compose run -e DIVESAFE_ENVIRONMENT=development` **alone does not skip the hash**: a
    bypass takes both a development environment and the flag. Each is a separate deliberate act.
    The compose file never sets the flag (it is not an environment setting: no key for it exists).
    Tests: with `DIVESAFE_ENVIRONMENT=production`, the flag does not skip the hash; with
    `development` and no flag, `load` and `approve` still refuse; with both, they proceed (and
    store the computed hash, as ADR 0022 decision 7 says). Mutations: skip the hash when the
    environment is not production (without the flag); skip it when the flag is given in production;
    read the flag from an environment variable; each must fail. This still does not stop an
    operator who overrides the environment **and** passes the flag (gap 4).
- **What each command writes (ADR 0022 decision 11).** `load` writes the documents, the chunks and
  a **DRAFT** event only. `approve` and `withdraw` are separate commands, in separate invocations.
  **Rule for the operator:** run `approve` only after the dive professional has recorded approval of
  that manifest hash in the private approval record, and supply the hash from that record; the
  operator does not compute it. The command cannot check this (gap 2).
- **`withdraw` (owner decision, 2026-10-21): exempt from the global lock, its own service.**
  `withdraw` is the only command that tightens, so nothing shared with a load may block it. It runs
  from `kb_withdraw`, which has **no corpus mount**, so a missing or wrong corpus path cannot stop
  it, and it takes **only the per-document advisory lock** (inside the status-event trigger), **not
  the global `kb_load` lock**, so a stuck or long load cannot make it fail on `lock_timeout`.
  **This is a deliberate departure from ADR 0022 decision 11**, which has each subcommand take the
  global lock; ADR 0022 is annotated (decision 11 and its test list) and **`risk-reviewer` must look
  at it.** Safety argument, for the reviewer to check: a WITHDRAWN event is final whatever its order
  (ADR 0022 decision 3), the trigger validates it under the per-document lock, and the global lock
  exists to serialise the per-site chunk-bound check and the version inserts of loads, which a
  withdrawal does not do.
  - Refusals that **apply**: the argument grammar (ADR 0022 decision 6), the TLS requirement on the
    connection, that the version exists, and fixed messages that never echo input.
  - Refusals that **do not** apply: the manifest and corpus checks, the manifest hash, the per-site
    chunk check and the global lock. The loader's privilege self-check (ADR 0022 decision 11)
    **still applies**, because a wrongly privileged credential should not be used to write
    (**`risk-reviewer` to decide whether `withdraw` should skip it**; if it did, a damaged loader
    role could not even tighten).
  - **Fallback when `withdraw` cannot run** (loader role damaged or missing, self-check refusing, a
    compose or image fault): (a) as `divesafe_owner`, insert the WITHDRAWN event with `psql` in the
    `db` container using `docker exec` and the in-container password read (the shape of
    `in_db_owner`; no compose, so no interpolation). The triggers still validate and stamp the
    event. The exact INSERT is written in the runbook when the table exists; or (b) stop the API
    (`docker compose ... stop api`) until `withdraw` works. **Recorded as gap 5.**
  - **Pins.** The test that `withdraw` succeeds while another session holds the global lock, and
    its mutation (make `withdraw` take the global lock), are in ADR 0022's test list. The compose
    pin is that `kb_withdraw` has no `volumes` and no corpus variable (A5).
- **Compose and the corpus path (owner decision, 2026-10-21: keep `${VAR:?}`).** Checked with
  Compose v5.5.1 on this machine: **an unset `${VAR:?}` fails every compose command that reads the
  file** (`config`, `up --no-start`, `run`) **even for a service whose profile is off.** So once the
  compose file has `${DIVESAFE_CORPUS_DIR:?...}`, **every** compose command on the production
  file, including `up -d --wait db` in the restore runbook and the ordinary `up`, needs the
  variable. Therefore:
  - `docker/production.env.example` gets a clearly fake placeholder line with a comment, for
    example `DIVESAFE_CORPUS_DIR=/set/me/outside/the/repository` (**described here; the example
    file and the compose file are not edited by this amendment**).
  - **The owner's existing `docker/production.env` needs the same variable before any compose
    command works after the compose change lands.** Otherwise `up`, `down` and the restore runbook
    stop with a `required variable` error.
  - A placeholder value defeats "a forgotten value stops the run"; the loader then refuses because
    the manifest is not at the mount, and the optional host wrapper (below) rejects the
    placeholder. A5 adds a scan that every `:?` variable name in the compose file appears in the
    example file.
- **"Outside the repository" cannot be checked in the container.** The container sees only the
  mount target, not the host's working tree, so ADR 0022 decision 13's "`kb_load` refuses a
  manifest root inside the repository" **cannot work as written**, and the compose pin sees only
  the text `${DIVESAFE_CORPUS_DIR:?...}`, not its value. The controls are **operator discipline and
  the `.gitignore` entry**. An optional **host-side wrapper script** may refuse a path inside the
  repository (and the placeholder) before it calls `docker compose run`; it is bypassable by
  running compose directly. ADR 0022 decision 13 is reworded to say this.
- **A deliberate override bypasses the check.** `docker compose run` can override the environment,
  the entrypoint, volumes, the network and the user. The pins test the file, not what an operator
  passes. The development flag makes skipping the hash need **two** deliberate acts, not one; it
  does not stop a determined operator.
- **Failure behaviour.** For `load` and `approve`: a missing corpus mount, a missing or malformed
  hash, a hash that differs, a failed self-check (ADR 0022 decision 11), or the global lock not
  obtained in time exits non-zero with a fixed message that does not echo the supplied value, and
  writes nothing. **A run with no arguments exits non-zero and writes nothing** (test). A stack
  started with `up` never runs either service.
- **Pins** (`tests/safety/test_production_compose.py`; see A5). Mutation checks as in A5.
- **Documented gaps.** (1) The hash check is **client-side**: it catches mistakes such as approving
  the wrong version; an operator in bad faith, or the loader credential used through `psql`, is
  caught only by process controls (ADR 0022 condition 4). (2) **The loader can approve its own
  load technically**: the credential can insert an APPROVED event. Approval is a process control by
  the dive professional, recorded in the private approval record; **this ADR does not name a path
  for that record** (the owner and the dive professional choose it, ADR 0022 "Still open"), so the
  control is not yet defined. (3) The corpus mount is outside the repository by operator discipline
  and the `.gitignore` entry. (4) Anyone who can run `docker compose` or `docker run` on the host
  can run the service with another mount, environment, flag or entrypoint. (5) The manual
  `withdraw` fallback relies on the owner credential and on the operator knowing the INSERT.

### A4. The `SECRET_FILES` entry and the API's refusal

- **What changes.** `config/secret_files.py` gains one name in `SECRET_FILES`: `loader_database_url`
  (file `divesafe_loader_database_url`, setting `DIVESAFE_LOADER_DATABASE_URL`), **single line
  (`False`)**. It is read by `kb_load`'s own narrow settings model (A3), with the production
  `sslmode` check (ADR 0022 decision 11), through the same loader of secret files as the other
  names.
- **The API never receives it, and refuses it.** The compose pin (A5) fails if the loader secret
  appears on `api`, `db` or `db-init`. In production the application also **refuses to start if the
  loader's connection string is in its own environment or in its secrets directory**, extending the
  existing refusal of `DIVESAFE_ADMIN_DATABASE_URL` in `api/state.py`, and refuses to start if its
  own credentials are the loader's (ADR 0022 decision 11). **To settle at code time:** the shared
  `Settings` must still know the name (or the API must scan the secrets directory for it) so the
  refusal can fire; the narrow loader model alone would not see it from the API side.
- **Amended statement.** The earlier sentence of this ADR that "only those five names are read" now
  reads: only the names in `SECRET_FILES` are read, **six once this amendment is built** (the five
  above and the loader's). A file named like any other setting is still ignored. Until it is built
  the list and its test stay at five.
- **Tests.** `tests/safety/test_secret_files.py`:
  `test_the_allow_list_is_exactly_these_five_names` becomes an exact set of the six names, never a
  prefix or a pattern, and its name is updated; the loader file is read, refused alongside an
  environment value of the same setting, refused when over 64 KiB or multi-line, and a stray file
  named like any other setting is still ignored. A test in the API start-up suite fails when the
  loader string is set in the production environment or present as a file. Mutations: add a seventh
  name; make the loader multi-line; drop the production refusal; drop the environment/file conflict
  check; each must fail.
- **Documented gap.** A secret in the loader file reaches only the loader services; the host's
  `docker/secrets` directory (0700, files 0444) remains the weak point it is for every other secret
  (see "Not solved").

### The API and knowledge on this stack

The stack's model server is `remote_external_opt_in`, so every knowledge item is **withheld** from
the prompt (ADR 0022 decision 15). The `DIVESAFE_LLM_SERVER_IS_LOCAL` pin in the compose tests now
also enforces that decision; **changing it needs both reviewers.** The app role gains `SELECT` on
the `kb_*` tables through `db_init`, and nothing else on them.

### A5. Production compose pins

New or changed tests in `tests/safety/test_production_compose.py`. None removes or loosens an
existing pin. The service-set pin (`{"db-tls", "db", "db-init", "api"}`) is changed to the exact set
of **six** (those four plus `kb_load` and `kb_withdraw`), never to a pattern. Each has a failure
condition; the mutation check is to break the compose file once and watch it fail.

1. **Under a profile only.** Both loader services have a `profiles:` entry, and no service
   `depends_on` either. Fails if a profile is removed (so `up` would start it).
2. **depends_on.** `kb_load` depends on `db` with `service_healthy` and on nothing else.
3. **Networks.** Both: `[data]`; `egress` is not named. Fails on `[data, egress]`.
4. **Mounts.** `kb_load` has exactly one volume, of the **form**
   `${DIVESAFE_CORPUS_DIR:?...}:<fixed target>:ro`. **The pin sees only the form, not the path**
   (A3): it fails on a missing `:ro`, a second volume, a Docker socket, a literal or relative host
   path, or a missing `:?`. **`kb_withdraw` has no `volumes` key at all** and no corpus variable.
5. **No ports.** No `ports` and no `expose` on either. Fails on either.
6. **Secrets exactly the loader one.** `_secret_targets` of each loader service equals the one
   name. Fails on any added secret, and on the loader connection string in any other service. The
   existing API pin (exactly four secrets) is unchanged and now also asserts it is absent.
7. **Environment.** `DIVESAFE_ENVIRONMENT == "production"` and the keys are exactly the expected
   few (`DIVESAFE_ENVIRONMENT`, `DIVESAFE_SECRETS_DIR`, the corpus-target setting; none for
   `kb_withdraw`'s corpus); no `DIVESAFE_LLM_*`, no key named like a hash, a manifest or the
   development flag. Fails on `development`, a missing key (ADR 0022 condition 1) and an added one.
8. **`db-init` environment.** Its keys are exactly `DIVESAFE_SECRETS_DIR`, `DIVESAFE_APP_DB_ROLE`
   and `DIVESAFE_LOADER_DB_ROLE`, the last a literal equal to the `pg_hba.conf` rule.
9. **Entrypoint.** One fixed exec-form list, **no `command` key**, no `sh -c`, no `${` in it.
10. **`restart: "no"`** on both.
11. **Hardening and image.** `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, no
    `cap_add`, `privileged`, `pid` or `user:`, and the same image, `build` block and no `target`,
    through the existing per-service tests extended to both.
12. **No approved hash or flag anywhere in compose.** No `manifest`, `sha256`, `approved` or
    development-flag key in either service's environment, secrets or mounts.
13. **The secret-name safety scan still passes** over the new services, and a new case feeds it a
    `DIVESAFE_LOADER_DATABASE_URL` literal and requires a failure.
14. **The `secrets:` map** gains exactly the loader entries (password and connection string). The
    **`db` service's secrets** equal its three plus the loader password.
15. **Example file.** Every `${NAME:?...}` in the compose file has a line for `NAME` in
    `docker/production.env.example` (A3). Fails when the corpus line is missing.
16. **API settings.** The `DIVESAFE_LLM_SERVER_IS_LOCAL` pin now also enforces ADR 0022 decision 15.
17. **Development flag.** The tests and mutations of the flag (A3) are the pin; compose cannot see
    the flag.

- **Documented gap.** Like the existing tests these read the layout of one file; a profile or
  mount written in a way the scan does not read (YAML anchors are already refused) is not seen, and
  the corpus path value is not seen.

### A6. Stage order

- **May be built before this amendment is made** (and are, per ADR 0022 "Conditions of acceptance"):
  the schema migration, `db_init` grants and its loader-role check, the loader code, and a
  **development-only** `kb_load` against a developer database with a developer-created role.
- **Wait for this amendment** (made, and reviewed by both reviewers): everything under `docker/`
  (the new script, `make-secrets.sh`, `pg_hba.conf`, the backup scripts) and the compose files, and
  therefore any production use. The knowledge connector, the prompt path, the record fields and
  the citation guard are later stages of ADR 0022 and are outside this amendment.
- **The owner's host must not run a stage-1 image, or rebuild production from a stage-1 commit.**
  Stage 1 has the `db_init` grants but not the role script, so `db-init` would fail on the existing
  volume. The first production load also waits for ADR 0022's open items and the hand run (A7).
- **Order on the owner's host once this is built:** add the corpus line to `docker/production.env`;
  take a backup with the **old** scripts; pull; `make-secrets.sh`; `up -d --wait db`; the role
  script; `up`; take a backup with the **new** scripts.
- **Failure behaviour.** Until the `docker/` changes exist, the production stack has no loader role
  and `kb_load` has no production path; `db_init` with the grants but no role fails with its fixed
  message, so the order must be: role script, then `db-init`.

### A7. Backup and restore

Today `docker/backup-common.sh` facts cover only `assessments`, `pending_runs`,
`run_failure_counts` and `schema_migrations` (13 fact keys, three guard triggers, a `hardening`
count). The dumps will also hold the **licensed corpus text** (ADR 0022 decision 13).

- **What changes.**
  - **Facts.** `facts_sql` gains `kb_*` row counts for `kb_documents`, `kb_chunks` and
    `kb_status_events`, and a **digest** of the knowledge tables, made like the `assessments`
    digest (ordered with the `C` collation, fixed formats, pinned `search_path`, names cast to
    text). **Columns spelled out:** `kb_documents` including `manifest_sha256`; `kb_chunks` the
    chunk **text** and `content_sha256`; and `kb_status_events` (document, version, status, the
    ordering key and the recorded-at in the fixed format). So a damaged archive that loses a
    WITHDRAWN event, alters a chunk or swaps a manifest hash produces a different count or digest
    and does not pass. `fact_keys`, `compared_facts`, `facts_complete` and `tables_line` are
    extended to the new keys and tables; the knowledge triggers are added to the shape check by
    name, all `ENABLE ALWAYS`.
  - **Manifest format bump.** `backup.sh` writes `format=2`; `restore.sh` refuses `format=1` with a
    message that says the archive is the old format. (Restore today requires `format=1` and exactly
    13 facts, so an old archive is refused by the count; the bump makes the message accurate.)
  - **Old archives.** An archive from before this change is recovered with the **old scripts**
    (commit `75cbd58`, or `433b569` for an archive from before migration 3). **The old scripts
    silently omit the `kb_*` checks**, so anything restored that way has knowledge rows that no
    fact covered; run the read-only post-restore check below.
  - **Restore order on a new volume.** `up -d --wait db`; the role script (A1); `restore.sh`;
    **re-apply withdrawals** made after the archive's `created_utc` (see R1 below): `up db-init`,
    then the withdrawals, then `up api`; then `up`. The role script comes **before** `restore.sh`.
    `restore.sh`'s "used database" check counts relations and functions in `public` only, so a role
    plus `GRANT CONNECT` does not trip it (read from the script). **The hand run must still cover
    this** (gap 1).
  - **R1: restoring an older correct archive can bring withdrawn text back.** A withdrawal made
    after the archive was taken is not in it, and the restored database shows that version as
    usable again. So: (a) the operator keeps a **withdrawal log** (version, time, reason) outside
    the database; (b) the restore runbook has an explicit step that re-applies every withdrawal
    dated after the archive's `created_utc`, **before the API serves**; (c) the restore's closing
    report **prints which knowledge versions are not WITHDRAWN** so the operator can compare it with
    the log; and (d) **a backup is taken right after any `withdraw`**, so the newest archive
    carries it. The log has no home yet (gap 3).
  - **Grants after a restore.** `pg_dump` without `--create` carries no database ACLs, so TEMP is
    back on PUBLIC and, more importantly, **`PUBLIC` has EXECUTE on the knowledge functions** until
    `db_init` runs; `db_init` revokes both on every run. The restore document says `db-init` is the
    next step and the closing report says the grants are not yet applied. The explicit TEMP revoke
    stays only as **extra protection**; `kb_load` refusing while TEMP is present (ADR 0022
    decision 10) is the fail-closed backstop.
  - **Read-only post-restore check.** After a restore, a read-only query lists documents, versions,
    manifest hashes and latest status, for the operator to compare with the approval record and the
    withdrawal log (ADR 0022 condition 5).
  - **Backup and restore refuse while a loader runs.** Both stop if a `kb_load` or `kb_withdraw`
    container exists, found by the compose service label (**to verify:** whether a `docker compose
    run` container carries that label; if not, a name or a lock check is used instead).
  - **`docs/backup-and-restore.md`** says in plain words that **dumps and archives contain the
    licensed corpus text**, that a licence revoked later cannot be honoured in an existing dump or
    off-machine copy, that the archive must be kept as the owner would keep the licensed sources,
    and the new fact count in place of "thirteen". Docs are written when the scripts are.
- **Golden-copy and script-review round.** `tests/safety/golden_backup/backup-common.sh.txt` and
  `restore.sh.txt` (and `backup.sh.txt` if it changes) are line-by-line change detectors, so these
  edits fail `test_backup_scripts.py` until someone reads the diff and updates the copies. That is
  a round of its own: the new SQL and the restore step are read by both reviewers, the golden
  copies are updated in the same change, and the scans in layers 2 and 3 of that test are extended
  (the new fact queries pin the search path and cast names; the fake `docker` is given the new keys).
- **Failure behaviour.** The backup refuses and keeps nothing when the knowledge facts differ
  between the live database and the scratch restore, when a knowledge trigger is missing or not
  `ENABLE ALWAYS`, or when a fact is missing. The restore refuses before the load if the manifest
  lacks the knowledge facts or they differ, and reports a mismatch after it. **A backup made before
  this change cannot be restored by the new scripts**: the restore refuses `format=1` with a message
  that says the archive is the old format (above). Order for an upgrade, as for migration 3: run
  `db-init` (which applies the knowledge migration), then take a backup with the new scripts. No
  production backup holding knowledge rows exists.
- **Pins.** (a) The golden copies. (b) A scan that `fact_keys` names the new keys and that the
  knowledge digest SQL contains the status-event table. (c) A fake-docker test where the scratch
  facts lack one status event and the backup keeps nothing; and the same for the restore.
  (d) A scan that the restore text names the TEMP revoke step, the `format=1` refusal, the
  withdrawal re-apply step and the not-WITHDRAWN report line, and that the digest names the columns
  above. (e) A test that backup and restore refuse while a loader container exists. Mutation
  checks: remove the status events from the digest (the lost-WITHDRAWN test must fail); drop
  `manifest_sha256` or the chunk text from the digest; drop a knowledge key from `fact_keys`; accept
  `format=1`; skip the TEMP step; each must fail. Without the line comparison the SQL checks are by
  text scan only, as for the existing facts.
- **Documented gaps.** (1) The fake `docker` cannot run SQL, so the content of the new facts, the
  role-before-restore order and the "used database" check are covered by scans and by a hand run on
  a throwaway volume, **which has not been done and is required before the first production load**.
  (2) The digest detects damage, not tampering, as today (the manifest sits next to its archive).
  (3) The withdrawal log has no defined home, so R1 depends on the operator keeping it. (4) The
  count and digest cover what is in the tables; they do not show that a text matches its approved
  manifest (ADR 0022 condition 5 is separate). (5) A restore that is not followed by `db-init`
  leaves `PUBLIC` EXECUTE on the knowledge functions and `kb_load` refusing (ADR 0022 decision 10).

### A8. Documented gaps recorded by this amendment

- **The loader credential can stall advisory locks.** Any loader connection can take the existing
  `_RUNS_LOCK_KEY` and `_LOCK_KEY` advisory locks and stall runs or migrations; stored per-role
  settings cannot cap it, because `hardening` requires none (ADR 0022 "Not solved"). The global
  `kb_load` lock also makes a stuck holder fail later loads on `lock_timeout`.
- **The loader can approve its own load technically**; approval is a process control by the dive
  professional (A3).
- **The manifest hash check is client-side** (A3).
- **The private approval record** has no path in this ADR, and the withdrawal log (A7) depends on it.
- **The loader role and password are not covered by the backup**, as the other roles are not
  (see the existing `docs/backup-and-restore.md` table): after a restore into a new volume the
  role script must be run again, before `restore.sh` (A7).
- **The loader password file stays mounted into `db` permanently** (owner decision; A1).
- **The corpus variable is required by every compose command**, so `docker/production.env` needs it
  (A3), and a placeholder defeats the stop-on-unset purpose.
- **The `pg_read_file` password design is unverified** (A1), as are the label check and the
  `depends_on` behaviour for `run`.
- **`withdraw` departs from ADR 0022 decision 11** (A3); `risk-reviewer` must look, including
  whether it should skip the privilege self-check.
- **Restoring an older archive can bring withdrawn text back** (A7, R1).
- **Nothing in this amendment is built or run.** Every pin above is a description of a test to be
  written, with its mutation check to be run when it is.

## Not solved

- **The knowledge loader (Amendment 1) is not built or run, and no reviewer has seen it.** Its new
  gaps are listed in A8; the feature does not ship until it is made and reviewed.
- **Longer model budgets (3000 tokens, 120 s call, 300 s stage) are not shown to work.** The stage
  limit is below three sequential calls at the call limit (3 x 120 s), so the risk proposal can be
  cut off; that fails closed (the rules' result stands). Calls hold the shared model slots for up
  to 360 s per run and cost roughly double the tokens, so consider a lower
  `DIVESAFE_MAX_RUNS_PER_ACTOR`. The web page stops polling at 360 s, the same as the run deadline,
  so a run finishing near it may show "outcome unknown" until reload (nothing may be acted on
  then). The re-review of this change found these and the missing timeouts (fixed).
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
- **No web UI, log shipping or monitoring.** Backups are condition 3. Nothing checks that a recent
  backup exists, and the manifest sits next to its archive, so it detects damage, not tampering.
  A decision made after the last backup is lost and that assessment is `PENDING_HUMAN` again; an
  assessment created after it does not exist; whoever acted on either must be told, and a dive
  that has already happened must not be decided again on the restored record
  (`docs/backup-and-restore.md`).
- **The restore loads an archive without the insert guard and without an authenticated actor.**
  Triggers are created after the data, which is the only way decided rows can load. The load runs
  as `divesafe_owner` (not a superuser), after a trial in a container with no network or secrets,
  and the manifest check catches damage. None of that makes a forged archive with a recomputed
  manifest detectable, and code planted in the schema (functions, constraints, triggers other than
  the guard) is restored and runs later. Only restore archives you made and kept safe.
- **The backup scratch check shares the host's kernel and memory.** It holds a full copy in memory
  in its own container (limited to 4 GB, 2 CPUs and a 2 GB scratch area); a host short of memory
  or disk can fail the backup or hurt the database. The 2 GB area and PostgreSQL's 1 GB limit on
  the digest string will make backups fail as the data grows, with a message that reads like
  damage. A hard kill can leave the scratch container, a `.part` file, the lock or the restore's
  private copy of the archive behind.
- **The scratch result cannot prove an archive honest.** Archive code runs there as that
  server's superuser and can make it print what it likes; the check catches accidents only.
- **The owner can still reach superuser-run code through trusted extensions** (their install
  scripts run with superuser rights; the image tag floats) and can change database settings.
  The manifest lists the extensions, the number of database settings (which must be zero) and a
  hash of every name in `public`, but nothing judges whether they are expected.
- **The guard functions are compared for sameness, not correctness.** The owner owns them; one
  replaced before the backup is preserved faithfully. A hash pinned from migration 1 is not built,
  and the definitions' text may differ across PostgreSQL major versions (then the trial refuses).
- **`pending_runs` and failure counters are compared by row count only.**
- **An archive loaded as the owner can still change the cluster.** `ALTER ROLE ... PASSWORD` on its
  own role needs no privilege; a stored role setting is counted (`hardening`) but a changed password
  is noticed only when the next connection fails. The guard definitions are not pinned to migration
  1: `db-init` does not re-apply it and `verify()` checks names, so a guard replaced before a backup
  survives a restore. The `ls` marker precedence on macOS was seen once; a directory swapped after
  the check is not detected.
- **The reconcile list needs the API log**, which compose keeps only for the current container.
- **A signed ruleset is not mounted**: `DIVESAFE_RULESET_PATH` is unset, so every assessment is
  INSUFFICIENT EVIDENCE until a dive professional's limits are signed and mounted.
- **The app role's password is set once.** Changing a password secret later does not change the
  role. The owner has no CREATEROLE and cannot change the app role's password, so this needs the
  superuser (`divesafe_bootstrap`, inside the `db` container): use `psql -X` with only that
  statement. The roles are created only on an empty volume.
- **An old volume keeps its old roles.** The role script runs only on an empty volume. A volume
  made by the first version of this stack would keep a superuser `divesafe_owner`, reachable over
  the network by `pg_hba.conf`; nothing reports it (`verify()` runs as the app role, and
  `migrate` does not refuse a superuser, because the development stack uses one). The first
  version was only ever run on throwaway volumes. The owner check below now reports it at
  start-up in production; recreate the volume.
- **Owner-superuser check (built, owner's choice, 2026-10-19).** In production `verify()` also
  reports a table owner that is a superuser or a member of one (`owner_not_superuser`, off by
  default so the development stack, whose owner is a superuser, is unaffected). It checks the
  owner of `public.assessments` only, at start-up only, and only on the PostgreSQL path: not the
  role behind `DIVESAFE_ADMIN_DATABASE_URL`, not the owners of the guard functions or other
  tables, and not an owner in `pg_execute_server_program` or `pg_write_server_files`. A
  production deployment whose `assessments` owner is a superuser is refused, Docker or not.
  Tested against test roles on PostgreSQL 17, not on the real image. `verify()` also pins
  `search_path = pg_catalog, pg_temp`, so a view in `public` cannot stand in for a catalog.
  Mutation check, first round: 19 mutants, 18 caught. The survivor ("a missing table passes")
  cannot be killed by a deterministic test: `verify()` returns early when the table does not
  exist, so the branch runs only if the table is dropped between two statements. After the
  review the query always returns a row and a missing table reads as a problem; the same
  mutation (that default flipped to "ok") survived again in round two for the same reason, so
  that line is reasoned, not tested. Round two (10 mutants on the review fixes): 7 caught and 3
  survived: that one, a redundant scan word (`trigger all`, now removed) and a real test gap
  (`ENABLE TRIGGER` on a non-guard trigger), which now has a test.
- **The guard triggers are `ENABLE ALWAYS` (migration 3, built 2026-10-19).** They fire in every
  `session_replication_role`, so a session, role, database or server setting of `replica` no
  longer switches them off (a superuser can still `DISABLE TRIGGER`; `verify()` reports a trigger
  that is not `ENABLE ALWAYS`). Migration 3 is the one reviewed exception to the word scan (ADR
  0015). Unit and integration tests ran on PostgreSQL 17; a hand run on the real stack
  (`pgvector/pgvector:pg16`, server 16.15, synthetic rows, a rebuilt `divesafe-api:local`) is
  recorded below.
  **A backup made before migration 3 cannot be restored by the scripts**: its triggers are plain
  `ENABLE`, and `backup-common.sh` now requires state `A`, so the check refuses it, with a
  message that says "does NOT match" and so reads like damage. Recovery: restore it with the
  scripts of commit `433b569` (the first with backup scripts), then run `db-init`, which applies
  migration 3. Order for an upgrade: run `db-init` before taking a backup with the new scripts,
  or back up with the old scripts before pulling. `backup.sh` on a version-2 database refuses
  with a message that names migration 3. No such production backup exists (the procedure has
  only been run on throwaway data).
  **The earlier condition 3 hand runs predate migration 3.** The sequence was repeated on the
  real stack (macOS host, `pgvector/pgvector:pg16`, synthetic rows, 2026-10-19) and gave:
  `db-init` applied migrations 1 to 3 on a fresh volume and all three triggers read `A`; the API
  started healthy with the production `verify()` including the owner check; as the
  `divesafe_bootstrap` superuser in replica mode, `DELETE`, `UPDATE` and `TRUNCATE` on
  `assessments` were each refused by the guard functions; as a control, with one trigger set to
  plain `ENABLE`, a replica-mode `UPDATE` went through (so the test can tell the states apart; the
  row was on a throwaway volume); `divesafe_owner` cannot set `session_replication_role` at all
  (permission denied); the backup wrote a manifest with three guard triggers, migration 3's
  checksum and `hardening=0`; with one trigger set to plain `ENABLE` the backup was refused and
  wrote no file, and `verify()` as the API reported the same trigger; a backup was restored into
  a new volume, the triggers read `A`, `db-init` reported the schema up to date, `--check`
  matched, and the API started again. **Not shown:** a Linux host, a restore of a pre-migration-3
  archive (the refusal path was reasoned from the script, not run), real data, decided rows with
  actual conditions, `pending_runs` rows, condition 1 (an assessment through the API with real
  secrets) and the model host. A trigger set back to plain `ENABLE` by hand makes every `db-init`
  fail with "not intact" and no migration repairs it: run
  `ALTER TABLE public.assessments ENABLE ALWAYS TRIGGER <name>` as the owner. `ENABLE ALWAYS`
  triggers also fire when a logical-replication subscriber applies changes (no replication is
  used here). `verify()` still
  does not compare trigger timing or function bodies, and does not report another trigger or rule
  on `assessments`. A later migration (reviewed, owner-run) could add a BEFORE trigger that runs
  after the guard; the API reads each row back and rolls back on a mismatch, so it fails closed on
  that path.
- **A setting changed after start-up is not seen.** `verify()` runs once, but every operation
  opens a new connection, so a superuser's `ALTER ROLE ... SET session_replication_role = replica`
  (or a reload after `ALTER SYSTEM`) takes effect at once and unseen. Since migration 3 that
  setting no longer stops the guard triggers; what remains unseen after start-up is a
  superuser's `DISABLE TRIGGER`, a grant or another setting changed later. Write-once is still
  enforced in code. Re-checking inside the write transaction would close it; not built.
- **`make-secrets.sh` is scanned only for a pipe into `write_new`.** A pipe through `tee` on that
  line is not detected.
- **A failed first start leaves a half-set-up volume** (recalled, not checked). If the role script
  fails, the image keeps the initialised data directory and skips the script on the next start,
  so some statements may be missing. Remove the volume and start again.
- **`verify()` is not the guard.** It is a start-up check; the guard is the triggers and the
  grants.
- **Upgrading PostgreSQL.** The image is pinned to major version 16. A volume is not compatible
  across major versions; moving to another major version needs a dump and restore
  (`docs/backup-and-restore.md`; a restore into a newer major version has not been run).
- **Port binding on older engines.** A port published to `127.0.0.1` was reachable from the local
  network on some older Docker Engine versions (recalled, not checked). State a minimum version
  once one is chosen.
- **Base images float.** `python:3.12-slim` and `pgvector/pgvector:pg16` are tags, not digests.
  The Dockerfile installs from PyPI without a lock file.
- **Not run:** a complete first run (one assessment went through; see condition 1 for what was and
  was not seen), the 30-day certificate
  renewal, and the stack on Linux (the run was on Docker Desktop for macOS, where file ownership
  and port publishing differ). On Linux the host reaches the published port through the bridge
  gateway too; what the API sees as the peer there was not observed.
- **Whether the postgres entrypoint leaves a password in a process environment** after reading it
  from a file was not checked.
- **`db_init` uses `print`** (AGENTS.md forbids it in `src`). It prints no secret.
- **The `psycopg-3.3.6-py3-none-any.whl` file in the repository root** (committed on 2026-10-10)
  is unrelated to this ADR and not used by any file here. Its purpose is unknown to this ADR.
