# Backup and restore of the production database

Part of ADR 0024 (PROPOSED). The `db-data` volume holds the only copy of every assessment and of
the named human decisions made on them. This is the procedure for copying it and for getting it
back. It was run on a developer machine (Docker Desktop, macOS) with synthetic rows and throwaway
secrets. It has not been run on Linux, with real data, or with an archive from another host.

## What is covered

| Covered | Not covered |
| --- | --- |
| Every table in the `divesafe` database: assessments (with human decisions and actual conditions), pending and failed runs, the migration record, and the knowledge tables (documents, chunks with their **licensed text**, status events). | The secrets directory (`docker/secrets`). Keep it separately and securely: without it you cannot start the stack. The database passwords are not in the backup, and new ones are fine for a restored database. |
| The schema, the three guard triggers, the nine knowledge guard triggers and their functions. | Roles and passwords (the role script creates them on a new volume), and grants (`db-init` re-applies them). |
| | The TLS volume `db-tls` (a new certificate is made on the next start). |
| | Anything outside the database: the signed ruleset, logs, `docker/production.env`. |

## The archive contains the licensed corpus text

Since ADR 0024 Amendment 1 (A7), **every dump and archive contains the knowledge corpus text**,
which is licensed from its sources (ADR 0022 decision 13). A licence that is revoked later
**cannot be honoured** in a dump or an off-machine copy that already exists: withdrawing the
document removes it from use, not from old archives. Keep archives as you would keep the licensed
sources themselves, and only where those licences allow.

## Who the scripts act as

**Neither script uses the database superuser.** Anything that the owner role or an archive put into
the database (a view over a function, an operator or aggregate in the `public` schema) can run
when a superuser reads it, so reading as the superuser is not harmless. The dump, the fact queries
and the restore all run as `divesafe_owner`, which owns the tables and is not a superuser.
They connect from inside the `db` container; the password is read there, so it is on no command
line and in no file or variable of the host. The fact queries also pin the search path to the
system catalog and cast names to text, so nothing in `public` can stand in for the built-in
functions the queries themselves use. (The archive's own code, and what the owner has put in the
database, still run with the owner's rights when the API and the owner use them.)

The first load of any archive (the backup's check and the restore's trial) happens in a
**throwaway container** started from the database image with no network, no secrets, no bind
mounts (it has a 2 GB in-memory scratch area), a read-only root, no capabilities, memory limited
to 4 GB and 2 CPUs. The archive's code runs there as that server's own superuser. A container
shares the host's kernel, so this limits what an archive can reach; it is not a sandbox against a
determined attacker.

## Make a backup

You need access to the Docker engine on the host, which is root-equivalent there, so run this as
the person who already operates the stack. The API may stay up. Two cautions:

- A backup taken to **move or upgrade** the database must be taken after you stop the API, or
  decisions made during it are lost.
- **Upgrading across migration 3** (guard triggers become `ENABLE ALWAYS`): run `db-init` before
  taking a backup with these scripts, or back up with the old scripts before you pull. These
  scripts refuse a database or an archive whose triggers are plain `ENABLE`. To restore an old
  archive, use the scripts of commit `433b569`, then run `db-init`.
- The scratch check needs free memory on the host (the archive is held in memory in its own
  container). The scratch area is 2 GB, and the fact query builds one string of all assessments,
  which PostgreSQL limits to 1 GB. Both fail closed, but the message ("does NOT match … or could
  not be loaded") reads like damage. As the data grows towards those sizes, backups will start to
  fail, and the scripts need changing then.

        docker/backup.sh /path/to/backups

- **Upgrading across migration 4** (the knowledge tables): run `db-init` first (it applies the
  migration), then back up with these scripts. The scripts before this change cannot back up a
  database with knowledge tables, and these scripts refuse a database without them.
- **Take a backup right after every `approve` or `withdraw`** (ADR 0024 A7, R1), so the newest
  archive carries it.
- Backup and restore **refuse while a `kb_load` or `kb_withdraw` container exists** in the project,
  running or stopped (found by its compose service label; `compose run` containers carry it). Run
  the loader with `--rm`, or remove a leftover container (`docker ps -a`). **If `docker ps -a`
  itself fails, they refuse too** ("could not list ... containers"); an error is never read as "no
  loader".
- **That loader check is a point-in-time check, not the protection.** It looks once, before the
  work starts; a loader started a moment later, under another compose project name or with plain
  `docker run` is not seen. What actually protects the archive is the before/after facts check
  below (a write in between makes it dump again) and the loader role itself, which can only
  connect and add knowledge rows. **A scheduled backup needs an alert on failure**: a leftover
  loader container makes every run refuse until someone removes it, and a silent refusal means
  no backup.

- The directory must be outside this repository, a real directory of yours with mode 0700 and no
  ACL entry (it is created with mode 0700 if missing). On a Mac, `ls -lde` must succeed and show no
  entry, so an ACL is seen even when extended attributes make `ls -l` print `@` instead of `+`. On
  Linux only the `+` that `ls -l` prints is checked (not run there). Do not put it in a folder that is synced to a
  cloud service (on a Mac, Desktop and Documents are synced when iCloud Drive is set to do so).
  A lock directory (`.backup.lock`) stops two runs at once; a killed run leaves it and the script
  says so.
- It prints the compose project and volume it is acting on. It refuses if more than one container
  labelled as a compose `db` service is running, whatever its project or image (an old stack beside
  a restored one), because that is how a backup is taken of the wrong database. It lists each
  one with its project. The development stack's `db` has the same service name, so it also
  causes a refusal while it runs.
- It writes `divesafe-<UTC time>.dump` (a `pg_dump` custom-format archive) and
  `divesafe-<UTC time>.manifest`, both mode 0600, as `.part` files first.
- **The facts in the manifest are read from the live database**, before and after the dump. If they
  differ, a write happened meanwhile and it dumps again (three tries). Equality is a sound check
  for `assessments` because the guard only lets rows be added and a first decision or first actual
  conditions be appended: the content only grows, so the same facts before and after mean the same
  content in the dump. That argument depends on the guard holding during the backup. For
  `pending_runs` and the failure counters only the number of rows is compared (a row can be
  added and removed in between without the number changing). On a database with steady writes
  all three tries can fail: nothing is kept and nothing is damaged, but there is then no backup
  until you stop the API for a moment.
- The nineteen facts are: the number of assessments and of decided ones; a SHA-256 digest of every
  assessment (id, creation time in UTC in a fixed format, full record including the human decision
  and actual conditions, in `C` collation order); the number of pending runs and of failure
  counters (counts, not values); the applied migrations with their checksums; the names of the
  guard triggers that are `ENABLE ALWAYS` (an archive made before migration 3, whose triggers are plain `ENABLE`, is refused); a hash of the definitions of the guard functions and triggers; the
  number of knowledge documents, chunks and status events; a SHA-256 **knowledge digest** of every
  column of the three knowledge tables (documents including `manifest_sha256`; chunks including the
  text and `content_sha256`; status events with document, version, status, ordering key,
  `hash_checked` and the recording time in UTC in a fixed format; one JSON array per row, `C`
  collation order), so an archive that lost a WITHDRAWN event, altered a chunk or swapped a
  manifest hash does not pass; the names of the nine knowledge guard triggers that are `ENABLE
  ALWAYS` and a hash of the knowledge functions' and triggers' definitions; the seven
  tables and that each is an ordinary table; the installed extensions; the number of settings
  stored for the database; a hash of the names of the relations and functions in `public` (only
  those two kinds); and `hardening`, a count that must be 0: schemas other than `public`, row-level-security policies, tables
  in `public` with row-level security switched on, default privileges, settings stored for any
  role, rules on tables in `public`, and `divesafe_owner` being a superuser (it is not one on a
  volume this stack created; a volume from the first version of the stack has a superuser owner and
  is refused). The queries run with `row_security = off`, so a policy makes them fail instead of
  hiding rows. All nineteen must be present on both sides: a query that fails the same way on both
  cannot pass as "equal". The backup stops if the three guard triggers or the nine knowledge guard triggers are not all ENABLE ALWAYS, if a
  table is not an ordinary table, or if any setting or `hardening` count is not zero.
- **Before it keeps the archive** it loads it into the throwaway container and requires the same
  facts there. If not, nothing is kept. That check catches accidents. It cannot stop an archive
  from making the throwaway server print whatever it likes, so it does not prove an archive is
  honest.
- The manifest also records the archive's SHA-256.

**What the facts do not cover:** the content of `pending_runs`, the values of the failure
counters, the position of the knowledge ordering sequence, what other functions do, table grants, casts and operators, and whether the guard functions are
*correct* rather than unchanged (the owner owns them and could have replaced them before the
backup; everything then agrees with the replacement). Only the names and the definitions' hash of
the guard are checked.

**The knowledge guard functions are different:** their bodies are **hash-pinned in the code** and
checked at start-up, by `db-init` (`migrate()`) and by the API's `verify()`, against a pin per
PostgreSQL major version (ADR 0022 condition 9). That pin, not the backup facts, is what refuses
a replaced knowledge guard. The `kb_guard_defs` fact only shows that the archive and the database
agree with each other.

**The archive holds named human decisions and assessment content.** Treat it like the database:
keep it off shared places, and encrypt it before it leaves the machine. Encryption, an off-machine
copy and retention are yours; the scripts do none of them. A backup that lives only on the host
that holds the volume does not survive losing the host.

**Frequency (owner decision, 2026-10-19): a backup every day.** That is the most history the
owner accepts losing: after a restore, every assessment and human decision made since the last
good backup is gone, up to about a day of them (more if a daily run fails and nobody notices).
**Nothing runs `backup.sh` on a timetable yet.** The scripts do not schedule themselves, and no
cron entry, timer or monitor is part of this repository, because the host, the account that may use
the Docker engine, and the place the archives go are the operator's. When you schedule it:
- Run it as the person who already operates the stack, with a directory that passes the script's
  checks, and keep the exit status. `backup.sh` exits non-zero and writes no archive when anything
  is off (a refused check, a database that kept changing, a scratch mismatch). A scheduler that
  discards the status turns a failing backup into a silent one, so alert on a non-zero exit **and**
  on the newest archive being older than a day and a half.
- A backup that fails is not retried by the script; the next day's run is the retry. Look at a
  failure the same day, because a second miss means two days of decisions at risk.
- Retention, encryption and the off-machine copy are still open (see Not done). Daily archives
  fill the disk over time, and each one holds named decisions.

## Restore

A restore goes into a **new, empty** database. For an archive made by `backup.sh` it does not write
over data in `public`; it is not a way to roll back. (An archive made by hand runs as the owner and
can also change the cluster: its own role's password and settings, and schemas other than `public`,
which the emptiness check does not look at.)

Before you remove or replace anything:

1. **Save the API log** (`docker compose ... logs api`) somewhere private. Compose keeps only the
   current container's log, so if the container was recreated since the backup (`up --build`, an
   image change), the log of what happened before is gone, and nothing else records it.
2. **Keep the old volume** until the restored one has matched its manifest and you have done the
   reconciliation below. To keep it, restore into another compose project: set
   `DIVESAFE_COMPOSE_PROJECT=<name>` for the scripts and use the same `-p <name>` for compose.
   Stop the old database first; the scripts refuse to run while two containers labelled as a compose
   `db` service run.
3. After a restore into another project, **that project is the production one**: set the same
   variable for every later backup, and start the API in that project only. Anything that starts
   the old stack again will serve stale data.

Then:

4. Stop the stack. If you are not keeping the old volume, remove `<project>_db-data` (the scripts
   print its exact name). Keep the secrets directory.
5. Start only the database: `docker compose -f docker/docker-compose.production.yml --env-file
   docker/production.env up -d --wait db`. On an empty volume this creates the owner and app roles
   and an empty `divesafe` database. **Do not start `db-init` or the API yet**: if either creates
   the schema while the restore is checking, the load fails on the existing tables.
   **This needs `DIVESAFE_CORPUS_DIR`** (every compose command on the production file does, even
   for a service whose profile is off; ADR 0024 A3). If `docker/production.env` is lost or lacks
   it, put the line back first, or give it on the command line for this one command
   (`DIVESAFE_CORPUS_DIR=/an/absolute/dir/outside/the/repository docker compose ...`; a variable
   in the shell wins over `--env-file`; an empty directory is enough, no loader runs here).
   While compose cannot read the file, use plain `docker` for what you must do anyway: `docker
   stop <name>` to stop a running API (`docker ps` shows the name).
6. Create the loader role with the role script (`docs/knowledge-loader.md`), **before** the
   restore. A role and `GRANT CONNECT` do not count as objects in `public`, so the restore still
   sees an empty database (read from the script; part of the hand run, not yet done).
7. `docker/restore.sh /path/to/divesafe-<time>.dump`. Its closing report lists every knowledge
   version that is **not WITHDRAWN** (with its latest status and manifest hash) and every approval
   with `hash_checked = false`, and says the grants are not yet applied.
8. `... up db-init` (the grants and revokes, including `EXECUTE` on the knowledge functions, which
   `PUBLIC` holds until then). The API must stay stopped.
9. **Re-apply withdrawals by state, not by time:** every version in the withdrawal log that the
   report shows as not WITHDRAWN, with `kb_withdraw` (`docs/knowledge-loader.md`). A version that
   is already WITHDRAWN gets its own message and nothing is written, so this can be repeated. The
   backup time is taken before the dump, so a withdrawal logged a little earlier may be in the
   archive or not: compare with the report, never with the clock. A v2 approved after the archive
   is simply absent and a superseded v1 governs again: re-load v2, or withdraw v1. **Nothing stops
   the API starting before this step** (operator discipline). The withdrawal log has no agreed
   home yet; the first production load waits for one.
10. Reconcile (below), then start the API: `... up -d --wait api`. `db-init` checks the schema
    again ("schema is up to date"), and the API starts only if its start-up checks pass.
11. Take a backup.

**An archive from before the knowledge tables (`format=1`) is refused** with a message that says
it is the old format. Restore it with the old scripts, run from a `git worktree` of the commit they
come from (`restore.sh` sources `backup-common.sh` from its own directory), into a database without
the knowledge tables, then **run the role script (step 6, from this checkout) before** `db-init`
(today's `db-init` refuses with `LOADER_ROLE_MISSING` without the loader role), then run `db-init`: commit `433b569` for an archive from before migration 3,
and for later ones commit `915285c` (the scripts are unchanged from `6840d2b` through `75cbd58` to
`915285c`: `git diff` of the three scripts between them is empty, checked 2026-11-09).

`restore.sh` refuses to run when:

- the manifest is missing, is format 1 (old) or not format 2, lacks any of the nineteen facts, does
  not list the three guard triggers, the nine knowledge guard triggers, the seven tables, no database setting and `hardening=0`, or the archive's SHA-256 differs from it (both files are copied
  to a private temporary directory first, and the copies are what is read);
- more than one container labelled as a compose `db` service is running;
- the API is running;
- a `kb_load` or `kb_withdraw` container exists in the project;
- the database already has any table, view, sequence or function in `public` (the message names
  the project and says not to remove a volume that holds data);
- the archive, loaded in the throwaway container, does not reproduce the manifest's facts.

Only then does it load the archive, as `divesafe_owner`, in one transaction
(`--single-transaction --exit-on-error`, `--no-owner --no-acl`), so for an archive made by
`backup.sh` a failure leaves the database empty. (An archive made by hand can contain statements
that end the transaction early; that is one more reason to restore only your own archives.) Table
and sequence grants come only from `db-init`. `--no-acl` also drops every function ACL, and a
function with no ACL gets PostgreSQL's default: everyone may `EXECUTE` it. For the five
`SECURITY DEFINER` knowledge guard functions (`kb_no_update_delete`, `kb_no_truncate`,
`kb_documents_insert`, `kb_chunks_insert`, `kb_status_events_insert`), migration 4 revokes that
default. `db-init` only **checks** it and stops with "the knowledge guard is not intact after
migrating: a role other than the owner may EXECUTE ..." (found by the A7 hand run, 2026-11-11). So
right after the load `restore.sh` runs `REVOKE EXECUTE ... FROM PUBLIC` on each of the five, then
reads them back read-only: each must exist once, with an ACL whose only grantee is its owner. If
the revoke or the read-back fails, it stops and says not to start the API. (The three
`assessments_*` guard functions are not `SECURITY DEFINER`, keep the default ACL in the source
database too, and are not affected.) **The backup facts do not cover function ACLs:**
`guard_defs` and `kb_guard_defs` hash `pg_get_functiondef`, which does not include the ACL, and the
scratch server also loads with `--no-acl`. So the trial load and both manifest comparisons match
whatever the ACLs are; this read-back is the check. Afterwards it reads the restored database and
compares it with the manifest again, then revokes `TEMPORARY` on the database from `PUBLIC` (extra protection only:
`10-create-app-role.sh` already revoked everything from `PUBLIC`, `db-init` does again, and the
loader refuses while `PUBLIC` holds TEMP). On a mismatch the data stays committed: it tells you not to start the API, and in that case you
remove **the restored** volume only (it names it) and start again.

If the script is interrupted after the load has committed, the final comparison did not run. A
re-run will say the database is not empty. Run `docker/restore.sh --check <dump>` and start the
API only after it says the database matches. If `--check` says it does NOT match on a database
that is empty, nothing committed (the load rolled back); run the restore again. `--check` writes nothing; while the API runs it
matches only until the next write, so it is a check of a restored database, not a health check.
A hard kill (`kill -9`) leaves a private copy of the archive in the system temporary directory;
remove it.

The append-only guard is never switched off: the data is loaded first and the triggers are
created after it, as part of a normal restore. **That also means a restore is the one way decided
rows get into the database without the insert guard and without an authenticated actor.** The
same holds for the knowledge tables: restored `kb_*` rows **skip the knowledge insert triggers**
(the checks and stamps they make on a normal insert); damage to them is caught by the `kb_digest`
fact, which covers every column of every row. The manifest check catches damage; it does not make
an archive trustworthy.

## After a restore: reconcile before anyone acts on the data

A restore returns the database to the moment of the backup. Everything since is gone, and the
system cannot tell you what it was.

- **Assessments created after the backup do not exist** (a request for one gets 404). They do not
  come back as `PENDING_HUMAN`.
- **A decision made after the backup is gone**; that assessment is `PENDING_HUMAN` again. So are
  actual conditions reported after it.
- **An assessment that was running** when the backup was taken, and finished afterwards, is shown
  as a run that did not finish. That is wrong for the real outcome and a client may already have
  acted on it. When it finishes, the script lists these runs (state `RUNNING` in the backup);
  they are exactly the runs whose outcome is unknown. **Keep that output.** The API marks them
  failed when it starts, silently; they then look like any other failed run, and are deleted after
  the retention period (`failed_run_retention_days`, 7 by default).
- A client that retries a request with the same `Idempotency-Key` after a restore can create an
  assessment with the *same id* and different content. Old logs and references to that id then
  point at a different record.
- Failure counters go back to their value at the backup.

What to do:

1. From the saved API log, list the assessment ids logged as `assessment stored`, `human decision
   recorded` or `actual conditions recorded` after the backup time. **The lists are not exact:**
   the backup time (`created_utc`) is taken before the dump starts, so some of those ids may
   exist in the restored database, decided or not. For each id, look it up in the restored
   database (or ask the API once it is safe to start it) before telling anyone it is gone. A
   line with `override: false` means the outcome was the restored record's own recommendation;
   otherwise **the log has neither the outcome nor the rationale**: you must ask the person. Tell
   everyone who acted on any of them. If the saved log is incomplete (the API container was
   recreated since the backup), you cannot list them: use only the waiting rule in step 2.
2. **Keep deciders out until the restored records can no longer be decided.** A restored
   `PENDING_HUMAN` record can be decided until `DIVESAFE_DECISION_MAX_AGE_MINUTES` after its
   creation. The restore prints `latest assessment created (UTC)` from the restored database:
   do not start the API with a decider's key until that time plus the limit has passed. To
   do something useful meanwhile without letting anyone decide, start the API without **every key
   that has the decider role** in `divesafe_api_key_hashes` (a person who also holds the assessor
   role then cannot create assessments either), or do not start it. The wait is safe only under the
   limit in force when the API starts: if `DIVESAFE_DECISION_MAX_AGE_MINUTES` is raised or unset
   later, restored records whose decisions were lost can be decided again. On a restore to another
   host, check that its clock agrees with the old one, or add a margin. Do not rely on telling
   each assigned decider the ids by name unless the log is complete.
3. **Do not decide again an assessment for a dive that has already happened.** A second decision
   would carry a new actor and time and could differ from the one that was acted on, and nothing
   would mark it as re-entered. Record what happened outside this system. Be aware of the cost:
   that record then shows no human decision for good, and actual conditions can never be attached
   to it (they require a decision). Keep the reconciliation list with the backups, and leave these
   ids out of any later analysis. For a dive that has not happened, make a new assessment.
4. A decision older than `DIVESAFE_DECISION_MAX_AGE_MINUTES` cannot be made at all, so in most
   cases step 3 is the only option.

How often to back up decides how much this is. It is the owner's decision and is not made here.

## What this does not protect against

- **The manifest guards against damage, not against an attacker.** It sits next to the archive, so
  someone who can change one can change the other, including a forged decision with a recomputed
  digest. To detect tampering, keep the manifest's digest somewhere the archive's holder cannot
  write.
- **Restore only archives you made yourself and kept safe.** The load is by a role that is not a
  superuser, after a trial in a container with no network or secrets. That is a limit, not a
  guarantee: the owner can install extensions the image marks as trusted (their install scripts
  run with superuser rights, and whether they are safe depends on the image's patch level; the
  image tag floats), and code that an archive plants in the schema (functions, views,
  constraints, triggers other than the guard) is restored and runs later with the API's and the
  owner's rights. The facts list the extensions, the database-level settings and the names of
  everything in `public`, so a changed set is visible in the manifest, but they do not judge
  whether it is expected.
- **A hand run showed some cases** (see ADR 0024, condition 3). They showed that no superuser is
  used, and that objects planted by the owner did not run in the database container. Because those
  objects only act for a superuser session, they do not show that the pinned search path alone is
  what protects the queries. They did not exercise every way an archive could be hostile.
- **A restored `pending_runs` table may hold runs marked as running** whose workers are gone.
  They are marked failed by their lease when the API starts. This comes from reading the code; the
  hand run had no pending runs in the data.
- **A killed run** leaves `.part` files (which hold real data) and the lock in the backup
  directory. Remove them. A hard kill of `docker` may also leave the scratch container running;
  `docker ps` shows it (it is started from the database image without a compose label).
- **The schema checks are exact.** The fact about the guard definitions is a hash of what
  `pg_get_functiondef` and `pg_get_triggerdef` print. A different PostgreSQL major version may
  print them differently; then the trial refuses, with no override. That fails closed, and is
  not tested across versions.

## Moving to a newer PostgreSQL major version

The image is `pgvector/pgvector:pg16`. A volume made by one major version cannot be opened by
another. Stop the API, take a backup, change the image (and `image=` in `docker/backup-common.sh`,
which a test keeps equal to the compose file), start `db` on a new volume or project and restore.
A `pg_dump` archive is restorable by a newer server (documented behaviour, not run here), but see
the last point above. Run the restore on a copy first, and keep the old volume until you have
compared.

## Knowledge rows for the A7 hand run

The A7 hand run (ADR 0024 A7) needs a database with knowledge rows, but `kb_load load` and
`approve` refuse on the production stack until the A6 gates are closed (exit 24, also with
`DIVESAFE_ENVIRONMENT` changed, because the loader string comes from the secrets directory). **Do
not lift that refusal for the hand run** (no `--entrypoint`, no edited module, no hand-made loader
string). Use the test-only seed instead. It writes synthetic rows directly, as `divesafe_owner`,
through the insert triggers, which still check and stamp every row. It never runs `kb_load`.

- `tests/hand_run/a7_seed_knowledge.sh` runs `tests/hand_run/a7_seed_knowledge.sql` with `psql`
  in the `db` container of the project named by `DIVESAFE_COMPOSE_PROJECT`. It refuses (exit 2,
  before any `docker exec`) unless the name is `throwaway-<lower-case letters, digits, ->` and does
  not contain `divesafe-prod`, and unless exactly one running `db` container has that project's
  label. The SQL refuses a database that already holds any knowledge row.
- It writes three documents, each version 1, all marked SYNTHETIC: `a7-approved` (DRAFT, APPROVED
  with `hash_checked = true`), `a7-withdrawn` (DRAFT, APPROVED with `hash_checked = false`,
  WITHDRAWN) and `a7-draft` (DRAFT). Four chunks. These cover the restore report's not-WITHDRAWN
  list and its `hash_checked = false` list.
- `tests/` is not in the image (`.dockerignore`), so the seed is run from the repository on the
  host. A test runs the SQL against PostgreSQL 16 and 17 test databases
  (`tests/integration/test_a7_seed_knowledge.py`); another runs the script's refusals against a
  fake `docker` (`tests/unit/test_a7_seed_script.py`).

Steps (on a developer machine with its own `docker/secrets` and `docker/production.env`, never on
the owner's production host). Every compose command goes through `$C`, which names the throwaway
project with `-p` and adds a test-only override that sets a **test image tag**; nothing here builds,
tags or runs `divesafe-api:local`, and nothing touches a `divesafe-prod` project. Never add
`--build` to a `$C` command. The work directory is outside the repository and is **never** the
production backup directory. Run the blocks in one `bash` session (zsh does not split `$C` into
words); `docker/production.env` needs `DIVESAFE_CORPUS_DIR` (an empty directory is enough).

```sh
# 0. A private work directory (mode 0700, outside the repository), a test image and its override.
W="$(mktemp -d)"; mkdir -m 0700 "$W/backups"
A7_IMAGE="divesafe-api:a7-test-$(date +%s)"
docker build -f docker/Dockerfile -t "$A7_IMAGE" .
cat >"$W/a7.override.yml" <<YML
services:
  db-init: { image: "$A7_IMAGE" }
  api: { image: "$A7_IMAGE" }
  kb_load: { image: "$A7_IMAGE" }
  kb_withdraw: { image: "$A7_IMAGE" }
YML
use() {  # use <project>: sets the project for the scripts and for $C
  export DIVESAFE_COMPOSE_PROJECT="$1"
  C="docker compose -p $1 -f docker/docker-compose.production.yml -f $W/a7.override.yml --env-file docker/production.env"
}
role() {  # the role script (docs/knowledge-loader.md, section 2) in THIS project's db
  $C exec -T -u postgres db sh -c 'PGPASSWORD="$(cat /run/secrets/db_bootstrap_password)" \
    exec psql -X -v ON_ERROR_STOP=1 -U divesafe_bootstrap -d divesafe -f -' \
    < docker/db/20-create-loader-role.sql
}
owner() {  # one read-only query as divesafe_owner in THIS project's db
  $C exec -T db sh -c 'PGPASSWORD="$(cat /run/secrets/db_owner_password)" PGSSLMODE=require \
    exec psql -X -At -v ON_ERROR_STOP=1 -h db -U divesafe_owner -d divesafe' <<SQL
$1
SQL
}

# 1. The source project.
use throwaway-a7
$C up -d --wait db            # new volume throwaway-a7_db-data
role                          # exit 0
$C up db-init                 # exits 0
tests/hand_run/a7_seed_knowledge.sh
                              # exit 0; last line: seeded documents=3 chunks=4 events=6
docker/backup.sh "$W/backups" # prints "backup written: ..." and "scratch restore equals the
                              # live database: assessments=0 decided=0"; the manifest in
                              # $W/backups holds all 19 facts, with kb_documents=3,
                              # kb_chunks=4, kb_status_events=6

# 2. Stop the source db: the scripts refuse while two compose `db` containers run
#    (single_database).
$C stop db

# 3. Restore into a second throwaway project: db, role BEFORE restore, restore, db-init.
use throwaway-a7-restore
$C up -d --wait db
role                          # the "used database" check must still see an empty database
docker/restore.sh "$W"/backups/divesafe-*.dump
```

Expected from `restore.sh`: "restored and matched the manifest: assessments=0 decided=0
kb_documents=3 kb_status_events=6" (all 19 facts equal); **no** line "TEMP could not be revoked" and
**no** line about the knowledge guard functions;
then the report, whose knowledge part is exactly:

```text
knowledge versions not WITHDRAWN (document version latest-status manifest-sha256); ...
a7-approved 1 APPROVED aaaa...a (64 a)
a7-draft 1 DRAFT cccc...c (64 c)
approvals with hash_checked = false (the manifest hash was not compared):
a7-withdrawn 1 <its recorded_at>
```

and finally "the grants are NOT yet applied. next, ...". Then:

```sh
owner "SELECT has_database_privilege('public', 'divesafe', 'TEMPORARY')"   # f (TEMP revoked)
owner "SELECT count(*) FROM pg_proc WHERE proname LIKE 'kb\\_%' AND proacl IS NULL"  # 0
$C up db-init                                                             # exits 0
owner "SELECT max(event_order) FROM public.kb_status_events"              # note it: M
$C --profile knowledge run --rm kb_withdraw a7-approved 1                 # exit 0, prints its
                                                                          # recorded_at, "withdrawn"
owner "SELECT event_order, status FROM public.kb_status_events
       WHERE document_id = 'a7-approved' ORDER BY event_order DESC LIMIT 1"
                                                                          # WITHDRAWN, above M
```

A withdrawal whose `event_order` is not above `M`, or that fails on `UNIQUE(event_order)`, means
the restored sequence was set back (ADR 0022 Known gaps): record it as a finding.

Cleanup, always, whatever failed (the API is never started in either project):

```sh
use throwaway-a7-restore; $C --profile knowledge down -v
use throwaway-a7;         $C --profile knowledge down -v
rm -rf "$W"                   # the hand-run archives, manifests and the override file
docker image rm "$A7_IMAGE"
docker volume ls -q | grep throwaway-a7 || echo "no volumes left"
```

The archives hold only synthetic rows, but they are still archives: keep them in `$W` only, never
in the production backup directory, and delete them with it.

## Not done

- **The hand run of the knowledge changes (ADR 0024 A7) on a throwaway volume:** a backup and a
  restore of a database with knowledge rows, the role-before-restore order and the "used database"
  check. The rows come from the test-only seed above, never from `kb_load`. The fake `docker`
  cannot run SQL; the fact queries were run once against empty PostgreSQL 16 and 17 test
  databases. **Required before the first production load.**
- The read-only post-restore check (documents, versions, manifest hashes and latest status) is
  the restore's closing report; there is no separate command to run it again later.

- A schedule that actually runs the daily backup (the frequency is decided: daily), a failure
  alert, a retention period, an off-machine copy and encryption (operator decisions).
- A run on Linux, a run against real data, a restore of an archive from another host, and a run
  with actual conditions or pending runs in the data.
- A restore drill on a timetable. Run one before relying on this, and again after any change to
  the schema or the images.
- Any automatic check that a backup is recent.
- A check that the guard functions are *correct* (a hash pinned from the migration; `db-init` does
  not re-apply migration 1 and `verify()` checks names only, so a replaced guard survives both),
  and a check that nothing but the expected objects exists in a restored database.
- A check of what an archive can do to the cluster as the owner: `ALTER ROLE ... PASSWORD` on its
  own role works for any role, and a stored role setting is counted, but a changed password is
  noticed only when the next connection fails.
