# Sourced by backup.sh and restore.sh (ADR 0024, docs/backup-and-restore.md). Not run on its own.
#
# No script here uses the database superuser. Anything the owner or an archive put into the
# database can run when a superuser reads it (a view over a function, an operator in `public`), so
# every query and the dump run as divesafe_owner, which owns the four tables and is not a
# superuser. They connect from inside the db container over TLS; the password is read there, so it
# is on no command line and in no file or variable of the host.
# The first load of an archive happens in a throwaway container with no network and no secrets,
# where the archive's code can only reach that container.

project="${DIVESAFE_COMPOSE_PROJECT:-divesafe-prod}"
case "$project" in
  '' | -* | *[!a-z0-9_-]*)
    echo "DIVESAFE_COMPOSE_PROJECT must be lower-case letters, digits, - and _" >&2
    exit 1
    ;;
esac

# Must be the image of the db service in docker-compose.production.yml (a test checks).
image=pgvector/pgvector:pg16

# What a database must look like before its content is compared.
guards_line='guards=assessments_append_only_guard,assessments_insert_guard,assessments_no_truncate_guard'
tables_line='tables=assessments:r,pending_runs:r,run_failure_counts:r,schema_migrations:r'
fact_keys='assessments dbsettings decided digest extensions failure_counts guard_defs guards hardening migrations objects pending_runs tables '

# The one running container of a compose service, found by label so that no compose settings are
# needed. More or fewer than one is an error.
one_container() {
  ids="$(docker ps -q --filter "label=com.docker.compose.project=$project" \
    --filter "label=com.docker.compose.service=$1")"
  count="$(printf '%s\n' "$ids" | grep -c . || true)"
  if [ "$count" -ne 1 ]; then
    echo "expected exactly one running '$1' container in project '$project', found $count" >&2
    return 1
  fi
  printf '%s' "$ids"
}

# Two running databases (an old stack beside a restored one, or the development stack) are how a
# backup is taken of the wrong one. Every container labelled as a compose `db` service counts,
# whatever its project or image, because an old container may run an older image.
single_database() {
  running="$(docker ps -q --filter label=com.docker.compose.service=db | grep -c . || true)"
  if [ "$running" -gt 1 ]; then
    echo "refusing: $running database containers are running (name, compose project):" >&2
    docker ps --filter label=com.docker.compose.service=db \
      --format '{{.Names}} {{.Label "com.docker.compose.project"}}' >&2
    echo "Stop the one you do not mean (docker ps), then run this again." >&2
    return 1
  fi
}

# in_db_owner <command> [args]: run the command in the db container as divesafe_owner over TLS.
# Needs $db.
in_db_owner() {
  docker exec -i "$db" sh -c 'PGPASSWORD="$(cat /run/secrets/db_owner_password)" PGSSLMODE=require exec "$@"' sh "$@"
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

# What a restore must reproduce, read in one snapshot. The search path is pinned so that nothing
# in `public` can stand in for a built-in; names are cast to text so no aggregate or operator on
# the `name` type can be chosen instead of a built-in one. Ordered by id with the C collation and
# written with a fixed timestamp format, so neither locale nor DateStyle nor time zone can change
# the digest. The guard functions and triggers are compared by definition, not only by name.
facts_sql() {
  cat <<'SQL'
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL search_path = pg_catalog, pg_temp;
SET LOCAL row_security = off;
SELECT 'assessments=' || count(*) FROM public.assessments;
SELECT 'decided=' || count(*) FROM public.assessments
  WHERE COALESCE(record->'human_decision', 'null'::jsonb) <> 'null'::jsonb;
SELECT 'digest=' || COALESCE(encode(sha256(convert_to(string_agg(
    id || '|' || to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '|' || record::text,
    E'\n' ORDER BY id COLLATE "C"), 'UTF8')), 'hex'), 'empty') FROM public.assessments;
SELECT 'pending_runs=' || count(*) FROM public.pending_runs;
SELECT 'failure_counts=' || count(*) FROM public.run_failure_counts;
SELECT 'migrations=' || COALESCE(string_agg(version::text || ':' || checksum, ',' ORDER BY version),
    'none') FROM public.schema_migrations;
SELECT 'guards=' || COALESCE(string_agg(tgname::text, ',' ORDER BY tgname::text COLLATE "C"), 'none')
  FROM pg_trigger
  WHERE tgrelid = 'public.assessments'::regclass AND NOT tgisinternal AND tgenabled = 'A';
SELECT 'guard_defs=' || COALESCE(md5(string_agg(d.def, E'\n' ORDER BY d.name COLLATE "C")), 'none') FROM (
  SELECT p.proname::text AS name, pg_get_functiondef(p.oid) AS def FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND p.proname LIKE 'assessments\_%'
  UNION ALL
  SELECT t.tgname::text, pg_get_triggerdef(t.oid) FROM pg_trigger t
    WHERE t.tgrelid = 'public.assessments'::regclass AND NOT t.tgisinternal) d;
SELECT 'tables=' || COALESCE(string_agg(relname::text || ':' || relkind::text, ',' ORDER BY relname::text COLLATE "C"), 'none')
  FROM pg_class WHERE relnamespace = 'public'::regnamespace
  AND relname::text IN ('assessments', 'pending_runs', 'run_failure_counts', 'schema_migrations');
SELECT 'extensions=' || COALESCE(string_agg(extname::text, ',' ORDER BY extname::text COLLATE "C"), 'none')
  FROM pg_extension;
SELECT 'dbsettings=' || count(*) FROM pg_db_role_setting
  WHERE setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database());
SELECT 'hardening=' || (
    (SELECT count(*) FROM pg_policy)
  + (SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace
       AND (relrowsecurity OR relforcerowsecurity))
  + (SELECT count(*) FROM pg_default_acl)
  + (SELECT count(*) FROM pg_db_role_setting)
  + (SELECT count(*) FROM pg_rewrite r JOIN pg_class c ON c.oid = r.ev_class
       WHERE c.relnamespace = 'public'::regnamespace AND r.rulename::text <> '_RETURN')
  + (SELECT count(*) FROM pg_namespace WHERE nspname::text NOT IN ('public', 'information_schema')
       AND nspname::text NOT LIKE 'pg\_%')
  + (SELECT count(*) FROM pg_roles WHERE rolname::text = 'divesafe_owner' AND rolsuper));
SELECT 'objects=' || md5(COALESCE(string_agg(o.k, E'\n' ORDER BY o.k COLLATE "C"), '')) FROM (
  SELECT 'r:' || c.relkind::text || ':' || c.relname::text AS k FROM pg_class c
    WHERE c.relnamespace = 'public'::regnamespace
  UNION ALL
  SELECT 'p:' || p.prokind::text || ':' || p.proname::text || ':' || p.pronargs::text FROM pg_proc p
    WHERE p.pronamespace = 'public'::regnamespace) o;
COMMIT;
SQL
}

# The facts of the real database (needs $db). The exit status is psql's.
live_facts() {
  facts_sql | in_db_owner psql -h db -U divesafe_owner -d divesafe -XAtq -v ON_ERROR_STOP=1
}

# scratch_facts <archive>: load the archive into a throwaway server in a container with no
# network, no secrets and a read-only root, as that server's own superuser, and print its facts.
# Whatever the archive contains runs there and nowhere else. Its limits are generous but finite.
scratch_facts() {
  docker run --rm -i --pull never --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 256 --user 999:999 \
    --memory 4g --memory-swap 4g --cpus 2 \
    --tmpfs /tmp:rw,size=2g,uid=999,gid=999,mode=0700 \
    -e "FACTS_SQL=$(facts_sql)" --entrypoint sh "$image" -ec '
export PGDATA=/tmp/pgdata
initdb -U scratch --auth=trust -D "$PGDATA" >/dev/null </dev/null
pg_ctl -D "$PGDATA" -w -l /tmp/pg.log -o "-c listen_addresses= -c unix_socket_directories=/tmp -c fsync=off" start >/dev/null </dev/null
createdb -h /tmp -U scratch divesafe_verify
pg_restore -h /tmp -U scratch -d divesafe_verify --no-owner --no-acl --single-transaction --exit-on-error
printf "%s\n" "$FACTS_SQL" | psql -h /tmp -U scratch -d divesafe_verify -XAtq -v ON_ERROR_STOP=1
' <"$1"
}

# The lines of the facts that must match exactly, in a fixed order.
compared_facts() {
  grep -E '^(assessments|dbsettings|decided|digest|extensions|failure_counts|guard_defs|guards|hardening|migrations|objects|pending_runs|tables)=' |
    LC_ALL=C sort
}

# facts_complete <facts>: exactly the thirteen facts, each once. A query that failed the same way
# on both sides cannot pass as "equal".
facts_complete() {
  keys="$(printf '%s\n' "$1" | cut -d= -f1 | tr '\n' ' ')"
  [ "$keys" = "$fact_keys" ]
}

# shape_ok <facts>: the three guard triggers, by name, all ENABLE ALWAYS; the four tables are ordinary
# tables; no setting is stored for the database; and `hardening` is 0: no row-level-security policy,
# no default privilege, no stored role setting, no rule on a table in `public`, no schema other than
# `public`, and the owner is not a superuser (each could change what a later session sees or may do).
shape_ok() {
  printf '%s\n' "$1" | grep -qxF "$guards_line" &&
    printf '%s\n' "$1" | grep -qxF "$tables_line" &&
    printf '%s\n' "$1" | grep -qxF 'dbsettings=0' &&
    printf '%s\n' "$1" | grep -qxF 'hardening=0'
}

# read_live: print the complete compared facts of the live database, or fail.
read_live() {
  raw="$(live_facts)" || return 1
  out="$(printf '%s\n' "$raw" | compared_facts)" || return 1
  facts_complete "$out" || return 1
  printf '%s\n' "$out"
}

# read_scratch <archive>: the same for the archive loaded in the throwaway server.
read_scratch() {
  raw="$(scratch_facts "$1")" || return 1
  out="$(printf '%s\n' "$raw" | compared_facts)" || return 1
  facts_complete "$out" || return 1
  printf '%s\n' "$out"
}
