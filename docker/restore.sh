#!/bin/sh
# Restores a backup made by backup.sh into a NEW, EMPTY database (ADR 0024,
# docs/backup-and-restore.md). It does not write over data in `public`; an archive that was not
# made by backup.sh can do more (see the document).
#
#   docker compose -f docker/docker-compose.production.yml --env-file docker/production.env \
#     up -d --wait db                      # on a new volume: creates the roles, no tables
#   docker/restore.sh /path/to/divesafe-<time>.dump
#   docker compose ... up -d --wait api    # db-init then reports the schema up to date
#
#   docker/restore.sh --check /path/to/divesafe-<time>.dump   # compare the database with the
#       manifest. Writes nothing. While the api runs it only matches until the next write: it is
#       a check of a restored database, not a health check.
#
# It refuses to run if the API is up, if the database already has any table or function in
# `public`, or if the archive does not match its manifest. Before anything is written, the archive
# is loaded into a throwaway server (no network, no secrets) and must reproduce the manifest. The
# real load runs as divesafe_owner, which is not a superuser, in one transaction. Afterwards the
# content is compared with the manifest again. The append-only guard is never switched off: a full
# restore creates the triggers after the data. Set DIVESAFE_COMPOSE_PROJECT to restore into another
# compose project and keep the old volume (stop the old database first). No script here uses the
# database superuser.
#
# If it is interrupted after the load has committed, the comparison did not run: run it again with
# --check and start the api only after that matches.
set -eu
unset CDPATH

here="$(cd "$(dirname "$0")" && pwd)"
. "$here/backup-common.sh"

check_only=0
if [ "${1:-}" = "--check" ]; then
  check_only=1
  shift
fi
dump="${1:?usage: restore.sh [--check] <backup>.dump}"
case "$dump" in -*) dump="./$dump" ;; esac
manifest="${dump%.dump}.manifest"
[ -f "$dump" ] || { echo "not a file: $dump" >&2; exit 1; }
[ -f "$manifest" ] || { echo "no manifest next to the dump: $manifest" >&2; exit 1; }

umask 077
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
trap 'exit 1' INT TERM HUP
cp "$dump" "$work/archive.dump"
cp "$manifest" "$work/manifest"

grep -qx 'format=1' "$work/manifest" || { echo "refusing: the manifest is not format 1" >&2; exit 1; }
expected="$(grep '^dump_sha256=' "$work/manifest" | cut -d= -f2-)"
actual="$(sha256_of "$work/archive.dump")"
if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
  echo "refusing: the archive does not match its manifest (damaged or replaced)" >&2
  exit 1
fi
want="$(compared_facts <"$work/manifest")"
if ! facts_complete "$want"; then
  echo "refusing: the manifest does not hold all thirteen facts" >&2
  exit 1
fi
if ! shape_ok "$want"; then
  echo "refusing: the manifest does not show the three guard triggers, the four tables, no settings and hardening=0" >&2
  exit 1
fi

db="$(one_container db)"
single_database
echo "project '$project' (volume ${project}_db-data)" >&2

if [ "$check_only" -eq 1 ]; then
  got="$(read_live)" || got=""
  if [ "$want" != "$got" ]; then
    echo "the database does NOT match the manifest (or its facts could not be read)" >&2
    exit 1
  fi
  echo "the database matches the manifest: $(printf '%s\n' "$got" | grep -E '^(assessments|decided)=' | tr '\n' ' ')" >&2
  exit 0
fi

if docker ps -q --filter "label=com.docker.compose.project=$project" \
  --filter "label=com.docker.compose.service=api" | grep -q .; then
  echo "refusing: the api is running; restore before starting it" >&2
  exit 1
fi

existing="$(printf '%s\n' \
  "SELECT (SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace" \
  "  WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','S','f'))" \
  "  + (SELECT count(*) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace" \
  "  WHERE n.nspname = 'public');" |
  in_db_owner psql -h db -U divesafe_owner -d divesafe -XAtq -v ON_ERROR_STOP=1)"
if [ "$existing" != "0" ]; then
  echo "refusing: the database of project '$project' already has objects in public ($existing)." >&2
  echo "It holds data: do NOT remove its volume (${project}_db-data) unless you have a backup you trust." >&2
  echo "Restore into an empty database: a new volume, or another DIVESAFE_COMPOSE_PROJECT." >&2
  exit 1
fi

trial="$(read_scratch "$work/archive.dump")" || trial=""
if [ "$trial" != "$want" ]; then
  echo "refusing: the archive, loaded in a scratch server, does NOT match its manifest (or could not be loaded)" >&2
  exit 1
fi

in_db_owner pg_restore -h db -U divesafe_owner -d divesafe --no-password --no-owner --no-acl \
  --single-transaction --exit-on-error <"$work/archive.dump"

got="$(read_live)" || got=""
if [ "$want" != "$got" ]; then
  echo "the restored data does NOT match the manifest (or its facts could not be read). Do not start the api." >&2
  echo "If this is the restored volume (${project}_db-data) and the old one is kept, remove only this one and start again." >&2
  exit 1
fi

echo "restored and matched the manifest: $(printf '%s\n' "$got" | grep -E '^(assessments|decided)=' | tr '\n' ' ')" >&2
report="$(printf '%s\n' \
  "BEGIN READ ONLY;" \
  "SET LOCAL search_path = pg_catalog, pg_temp;" \
  "SELECT 'latest assessment created (UTC): ' || COALESCE(to_char(max(created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), 'none') FROM public.assessments;" \
  "SELECT 'runs whose outcome is unknown (marked running in the backup), before the api marks them failed:';" \
  "SELECT id || ' ' || to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') FROM public.pending_runs WHERE state = 'RUNNING' ORDER BY created_at;" \
  "COMMIT;" |
  in_db_owner psql -h db -U divesafe_owner -d divesafe -XAtq -v ON_ERROR_STOP=1)" || {
  echo "restored and matched, but the latest-assessment time and the unknown runs could not be read." >&2
  echo "Do not start the api. Read them with psql as divesafe_owner first (docs/backup-and-restore.md)." >&2
  exit 1
}
printf '%s\n' "$report" | LC_ALL=C tr -cd '[:print:]\n' >&2
echo "next: reconcile (docs/backup-and-restore.md), then start the api (db-init re-applies the grants)" >&2
