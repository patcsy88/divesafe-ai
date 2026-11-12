# The knowledge loader on the production stack (ADR 0024 Amendment 1)

This is a procedure to run by hand. It loads, approves and withdraws site-knowledge documents
(ADR 0022) on the production compose stack. **The production load is on**: every gate in
ADR 0024 A6 ("First-production-load gates") is closed, or waived by the owner (licence evidence),
and `PRODUCTION_LOAD_GATES_CLOSED` in `src/divesafe/services/kb_load.py` is `True` (owner
decision, 2026-11-12). The refusal stays in the code: while that literal is `False`,
`kb_load load` and `kb_load approve` exit 24 ("refused: production load gates (ADR 0024 A6) are
not closed; nothing written") before reading the corpus or connecting, **in production and
whenever the loader string was read from the secrets directory**, whatever `DIVESAFE_ENVIRONMENT`
says (so `-e DIVESAFE_ENVIRONMENT=development` on the compose loader does not lift it).
`withdraw` is not affected. The only switch the code offers is a reviewed change of that literal
(owner decisions, 2026-11-09). **Switching the load off again:** a one-line reviewed change back
to `False`, then **rebuild and redeploy the image** (`up -d --build` of the main stack from that
commit); the running image keeps whatever the literal was when it was built.

**What the refusal is and is not.** It stops accidents and casual overrides. It does **not** stop
a determined operator with Docker access: one who replaces the entrypoint (`--entrypoint`), runs
a changed copy of the module, or puts a hand-built loader string in `DIVESAFE_LOADER_DATABASE_URL`
instead of the secrets directory can still write. Do none of these; the gap is recorded in ADR
0024 A8. **Copying the loader string out of the secret** (for example into `-e
DIVESAFE_LOADER_DATABASE_URL=...`) also exposes the loader password: it ends up in the process
arguments (`ps`), the shell history and the container's configuration (`docker inspect`). If that
ever happens, rotate the loader password (`ALTER ROLE divesafe_loader` as the superuser, then a
new `db_loader_password` and loader string, section 2). For the A7 hand run use the test-only seed in
[backup-and-restore.md](backup-and-restore.md) ("Knowledge rows for the A7 hand run"), never
`kb_load`.

Knowledge is context only; it never supplies live conditions and never decides a dive. On this
stack every knowledge item is withheld from the model (ADR 0022 decision 15).

Run everything from the repository root. `C` below is a shell function for the compose prefix
(a function, not a string: `C="docker compose ..."` with `$C` does not word-split in zsh, the Mac
default shell, and ran nothing on the owner's Mac on 2026-11-12; the function works in bash and zsh):

```sh
C() { docker compose -f docker/docker-compose.production.yml --env-file docker/production.env "$@"; }
```

Every compose command on the production file needs `DIVESAFE_CORPUS_DIR` in
`docker/production.env`, even `up` and `down` (ADR 0024 A3). Set it to an **absolute path** of a
directory **outside the repository** (a relative path is resolved against `docker/`, and a
directory inside the repository would put licensed text where `git` and the build can see it). The placeholder in the example file is refused by the loader (no manifest there).

## 1. Upgrade order on a host that already has a volume (ADR 0024 A6)

1. Add the `DIVESAFE_CORPUS_DIR=` line to `docker/production.env`.
2. Take a backup with the **old** scripts (before you pull).
3. Pull.
4. `sh docker/make-secrets.sh docker/secrets` (creates only the two missing loader files).
5. `C up -d --wait db` (recreates `db`: it now has the loader password secret and re-reads
   `pg_hba.conf`).
6. Create the loader role (section 2).
7. `C up -d --build` (without `--build` the old image runs the old `db_init`).
8. Take a backup with the **new** scripts.

## 2. Create the loader role

The bootstrap superuser runs `docker/db/20-create-loader-role.sql`, read from the host through
standard input. The password is read inside the container; no secret is on the command line:

```sh
docker compose -f docker/docker-compose.production.yml --env-file docker/production.env \
  exec -T -u postgres db sh -c 'PGPASSWORD="$(cat /run/secrets/db_bootstrap_password)" \
  exec psql -X -v ON_ERROR_STOP=1 -U divesafe_bootstrap -d divesafe -f -' \
  < docker/db/20-create-loader-role.sql
```

It is safe to run again: a clean existing role is left unchanged. It exits non-zero and creates
nothing when the role exists with an attribute, a membership or a stored setting (it is not
repaired), or when `docker/secrets/db_loader_password` is not exactly 48 lower-case hex characters
with no newline. It sets the password only when it creates the role: if you change the file later,
the role keeps the old password (run `ALTER ROLE` as the superuser yourself). Do not add `-e`,
`-a`, `-b` or `ECHO` to the psql command.

If `divesafe_loader_database_url` is missing but `db_loader_password` exists, `make-secrets.sh`
will not write it. If the role does not exist yet, remove both loader files and run
`make-secrets.sh` again. Otherwise write the string by hand from the password file, without
printing it, in exactly the form `make-secrets.sh` writes (one line, no trailing newline):

```text
postgresql://divesafe_loader:<password>@db:5432/divesafe?sslmode=require&channel_binding=require&connect_timeout=5
```

`<password>` is the 48 hex characters of `docker/secrets/db_loader_password`. The loader refuses
a string without `connect_timeout` or with a value above 5, and in production one whose `sslmode`
is not `require`, `verify-ca` or `verify-full` (it does not check `channel_binding`; keep it as
written).

## 3. Load, approve, withdraw

The loader services start only with the `knowledge` profile, never with `up`. **Never add
`--build` to these commands and never run `build` with the knowledge profile:** build the image
only from the deploy commit, with `up -d --build` of the main stack. (If the gate is ever shut
again, `load` and `approve` refuse with exit 24 on this stack; see the top of this page.)

```sh
C --profile knowledge run --rm kb_load load --approved-manifest-sha256 <hash from the approval record>
C --profile knowledge run --rm kb_load approve <document_id> <version> --approved-manifest-sha256 <hash>
C --profile knowledge run --rm kb_withdraw <document_id> <version>
```

- Run `approve` only after the dive professional has recorded approval of that manifest hash in
  the private approval record, and copy the hash from that record. Do not compute it yourself.
- `withdraw` prints the `recorded_at` of the WITHDRAWN event. Append a line to the withdrawal
  log, `/Users/patcsy88/DiveSafe/records/withdrawal-log.csv` (owner decision 2026-11-12; columns
  `document_id,version,recorded_at_utc,reason,withdrawn_by`; mode 600, folder 700, outside the
  repository, the database and its backups). The approval record is `approval-record.csv` in the
  same folder (columns `document_id,version,manifest_sha256,approved_by,approved_at_utc,notes`);
  append a line after every `approve`. **Format:** `approved_at_utc` is ISO 8601 UTC,
  `YYYY-MM-DDTHH:MM:SSZ` (`date -u +%Y-%m-%dT%H:%M:%SZ`), the time the dive professional
  approved, not when `approve` ran; `recorded_at_utc` is copied exactly as `withdraw` prints it;
  a field that contains a comma is double-quoted. A field that starts with `=`, `+`, `-` or
  `@` is read as a formula by a spreadsheet: prefix it with `'`, or do not open the file in a
  spreadsheet.
- **Re-loading a changed text** needs a new version: `"version": 2`, `"supersedes": 1`, a new
  file name, and a new approval line. `load` of an existing version is refused by the database
  (exit 11, "a new version must be greater than every existing version"); the message gives no
  hint, so: **exit 11 after a successful load usually means the version already exists; bump
  the version.**
- Take a backup right after every `approve` and `withdraw` (ADR 0024 A7, R1), and copy the file
  you just appended to next to the newest archive. Plain CSV, no integrity protection: the copy
  is the only other one.
- Licence and `display_permitted` evidence: waived for the MVP (owner decision 2026-11-12). The
  manifest fields are still required and the loader still refuses without them; the owner fills
  them in and carries the responsibility for the right to use each source (ADR 0022 Known gap 31).
- The hash is not a secret, but it is visible in shell history and in `docker ps` while the
  run container exists.
- Backup and restore refuse while a `kb_load` or `kb_withdraw` container exists. Use `--rm`.

### Building the manifest

`kb_manifest_build` writes the computed parts of `manifest.json` (file and chunk SHA-256s, byte
offsets, chunk numbers, heading gaps) from a draft you write. It is a convenience, not a check:
`load` re-checks everything. Write the draft (for example `draft.json`) in the corpus directory
with every human field, `null` included: top level `format`, `attests_no_generated_text`,
`attests_no_personal_names`, `documents`; per document every manifest field except `file`, plus
`path` (relative to the corpus directory; never `manifest.json`). Nothing is defaulted; a
missing, extra or repeated key is refused.

**Whoever writes `attests_no_generated_text: true` and `attests_no_personal_names: true` in the
draft is the one attesting**; the tool copies them and attests nothing. The dive professional
reviews and approves the generated `manifest.json` itself (its sections, spans and hash), not
just the draft.

Chunking: a line starting with `## ` is a heading, stored as a `heading` gap; its text is the
`section` of the chunks after it (before the first heading the section is the `title`).
Paragraphs (separated by blank lines) are packed into chunks of at most 1500 characters; a
longer paragraph is refused, never split. Exact rule: the module docstring.

Run it from `/Users/patcsy88/DiveSafe/app` (the production checkout). `DIVESAFE_CORPUS_DIR` is
set in `docker/production.env`, not in your shell, so read it first; write it there unquoted
(`DIVESAFE_CORPUS_DIR=/path`: the `sed` read keeps quotes, and a quoted value gives a bad mount
and a refusal). The manifest is written to a temporary file outside the repository and the corpus;
only if the build succeeded does it replace `manifest.json`, get hashed and get checked:

```sh
DIVESAFE_CORPUS_DIR=$(sed -n 's/^DIVESAFE_CORPUS_DIR=//p' docker/production.env)
: "${DIVESAFE_CORPUS_DIR:?}" && out=$(mktemp) && docker run --rm --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges -v "${DIVESAFE_CORPUS_DIR:?}":/corpus:ro \
  --entrypoint python divesafe-api:local -m divesafe.services.kb_manifest_build \
  /corpus/draft.json > "$out" \
  && chmod 644 "$out" && mv "$out" "${DIVESAFE_CORPUS_DIR:?}/manifest.json" \
  && shasum -a 256 "$DIVESAFE_CORPUS_DIR/manifest.json" \
  && docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges -v "${DIVESAFE_CORPUS_DIR:?}":/corpus:ro \
  --entrypoint python divesafe-api:local -m divesafe.services.kb_manifest_build --check /corpus \
  || { echo "stopped: read the message above and check manifest.json"; rm -f "${out:-}"; }
```

Never redirect the output into the repository (it holds licence and permission references and
section titles; `manifest.json` and `draft.json` are git-ignored as a backstop) or straight onto
the existing `manifest.json` (the shell empties it before a refused build). The tool never
writes a file: the manifest goes to stdout and a fixed message to stderr. The `shasum` line is
the hash for the approval record, taken **after** the file is written. `--check` runs only after
a successful build (a refusal leaves no new hash to compare): it runs the loader's `read_corpus`
and prints the manifest SHA-256 (it must match the `shasum` line) and each document's id, version
and chunk count. If the block stops after the `mv` (`shasum` or `--check` failing), the new
`manifest.json` is already in place: do not approve it until `--check` succeeds.

Before approval, also compare each document's `file.sha256` in `manifest.json` with
`shasum -a 256 "$DIVESAFE_CORPUS_DIR/<path>"` on the host; they must be equal. Docker Desktop's
file sharing can show a container stale contents of a file edited in place moments before (seen
2026-11-12: a build read the new file cut to the old file's length, and the chained `--check`
refused it). If they differ or `--check` refuses, wait a few seconds and run the block again.
The image must include the tool (built from a commit that has it).

Known gaps (2026-11-12 security reviews):
- K4: the build uses the loader's private `_split_chunks` and `_open_dir`, and its final check
  re-implements the `read_corpus` loop; a new check added to the loader reaches `--check` and
  `load` but not the build until the builder is updated. `--check` before approval covers it.
- K5: the draft and the document files each get the 1 MiB budget, so up to 2 MiB can be read
  before the shared limit is applied; `MAX_DOCUMENTS_PER_LOAD` is only applied in the final
  check, after every file is read.
- N1: `read_corpus` catches only `OSError` around `read_regular_file`; a lone-surrogate path
  would raise `UnicodeEncodeError`. Unreachable because `parse_manifest` (pydantic-core JSON)
  refuses lone surrogate escapes (checked 2026-11-12 for `\ud800`, `\udc00`, `\udcff`); not
  pinned by a test.
- N6: `.gitignore` covers only the names `draft.json` and `manifest.json`: a draft with another
  name is not ignored, and a future fixture with either name would be silently ignored.
- N7: Docker Desktop (macOS) file sharing can serve stale contents of a just-edited corpus file
  to a new container (2026-11-12, Docker Desktop 29.8.2). The build, `--check` and `load` all
  read through such a mount; if they all see the same stale bytes the hashes agree. The host
  `shasum` comparison above is the only check against that, and it is manual.

### First production load

The first real load is untested end to end (the A7 hand run 2 ran with the gate shut and used the
test-only seed, never `kb_load`). Run it as a supervised run, one document, with the new image
deployed first (a separate step) and the corpus directory holding that one document:

1. `load` it; expect one DRAFT version.
2. `approve` it with the hash from the approval record; append the approval-record line.
3. Create one assessment and check its record shows the `kb:` item **withheld**
   (`withheld_knowledge`, `egress_basis` not `declared_local`), nothing in a prompt.
4. `withdraw` it once, to prove the path; append the withdrawal-log line with the printed
   `recorded_at`.
5. Take a backup; copy both CSVs next to the archive.
6. Run the post-load check: the A7 closing-report SQL from `docs/backup-and-restore.md`
   ("The closing report did not run"), read-only as `divesafe_owner`; the version must be listed
   as WITHDRAWN and no approval with `hash_checked = false`.

If any step refuses, stop and record it; do not retry with a changed environment.

**Done 2026-11-12** (ADR 0024 A8 "Deployed and first load done"): `tioman-site-guide` v1 on
image `f4c8511e0163`; load, approve (`hash_checked = true`), four assessments with the item
withheld, withdraw, backup with both CSVs, closing report clean. Found on the way: a repeated
`load` gives exit 11 (above); the first assessment's model calls all failed on a model-host 404
with no alert (ADR 0027).

### When `withdraw` says "lock busy"

A load is holding that document's lock. (1) Stop the `kb_load` run container (the server rolls
the load back) and retry. (2) Or, as the bootstrap superuser, find the loader backend in
`pg_stat_activity` and run `pg_terminate_backend`. (3) Else use fallback (b) below.

### When `withdraw` exits 3

Since `91fa8fe` (2026-11-12) a loader string read from the secrets directory gets the production
DSN-shape and absolute-target rules in every environment, and `withdraw` shares that settings
loader: a relative `DIVESAFE_CORPUS_TARGET` makes it refuse with exit 3 although `withdraw` never
reads the corpus. An exit-3 `withdraw` is a configuration problem: fix the setting and re-run;
never skip the withdrawal. `make-secrets.sh` output already passes these rules, so a production
`withdraw` is unaffected.

### When `withdraw` cannot run at all

(The loader role is damaged or missing, a compose or image fault, or `DIVESAFE_CORPUS_DIR` is
missing.) Either:

(a) insert the WITHDRAWN event as `divesafe_owner`, with plain `docker exec` (no compose, so no
variable is needed). Replace `<db container>`, `<document_id>` and `<version>`; the id must fit
the document id grammar and the version must be a whole number. The triggers still validate and
stamp the event, and the same per-document lock applies:

```sh
docker exec -i <db container> sh -c 'PGPASSWORD="$(cat /run/secrets/db_owner_password)" PGSSLMODE=require \
  exec psql -X -v ON_ERROR_STOP=1 -h db -U divesafe_owner -d divesafe' <<'SQL'
BEGIN ISOLATION LEVEL READ COMMITTED;
INSERT INTO public.kb_status_events (document_id, version, status, hash_checked)
  VALUES ('<document_id>', <version>, 'WITHDRAWN', false)
  RETURNING to_char(recorded_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
COMMIT;
SQL
```

or (b) stop the API (`C stop api`, or `docker stop <api container>` if the function is not defined)
until `withdraw` works.

If `withdraw` exits 11 and no load of that family is running, the ordering sequence may have been
set back (ADR 0022 Known gaps). Fallback (a) fails the same way. Use (b), then as the owner
`setval` `public.kb_status_event_order_seq` above `max(event_order)` and run `withdraw` again.

## 4. Backup and restore

See [backup-and-restore.md](backup-and-restore.md). Archives contain the licensed corpus text.
After a restore into a new volume the loader role must be created again (section 2) **before**
`restore.sh`, and withdrawals made after the archive was taken must be re-applied from the
withdrawal log (`/Users/patcsy88/DiveSafe/records/withdrawal-log.csv`, or its copy by the
archive) before the API starts.
