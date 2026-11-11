#!/bin/sh
# ADR 0024 A7 hand run ONLY (docs/backup-and-restore.md, "Knowledge rows for the A7 hand run").
# Writes synthetic knowledge rows into the db container of a THROWAWAY compose project, as
# divesafe_owner with plain `docker exec`, from a7_seed_knowledge.sql next to this file. It never
# runs kb_load and never lifts the production load gate. It refuses:
#   - a project name that is not `throwaway-<lower-case letters, digits, ->` (so never
#     divesafe-prod or the default);
#   - unless exactly one running db container carries that project's label;
#   - a database that already holds any knowledge row (in the SQL).
# Not in the image: tests/ is in .dockerignore.
set -eu
unset CDPATH

project="${DIVESAFE_COMPOSE_PROJECT:-}"
case "$project" in
  throwaway-*) ;;
  *)
    echo "refused: set DIVESAFE_COMPOSE_PROJECT to a throwaway project named throwaway-<name>" >&2
    exit 2
    ;;
esac
case "$project" in
  throwaway- | *[!a-z0-9-]* | *divesafe-prod*)
    echo "refused: the project name must be throwaway-<lower-case letters, digits, ->" >&2
    exit 2
    ;;
esac

here="$(cd "$(dirname "$0")" && pwd)"
sql="$here/a7_seed_knowledge.sql"
[ -f "$sql" ] || { echo "refused: $sql is missing" >&2; exit 2; }

ids="$(docker ps -q --filter "label=com.docker.compose.project=$project" \
  --filter "label=com.docker.compose.service=db")"
count="$(printf '%s\n' "$ids" | grep -c . || true)"
if [ "$count" -ne 1 ]; then
  echo "refused: expected exactly one running db container in project '$project', found $count" >&2
  exit 2
fi

docker exec -i "$ids" sh -c 'PGPASSWORD="$(cat /run/secrets/db_owner_password)" PGSSLMODE=require \
  exec psql -X -v ON_ERROR_STOP=1 -h db -U divesafe_owner -d divesafe -At' <"$sql"
