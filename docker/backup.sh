#!/bin/sh
# Backs up the production database (ADR 0024, docs/backup-and-restore.md).
#
#   docker/backup.sh /path/to/backups
#
# The API may stay up, but a backup taken to MOVE or UPGRADE the database must be taken after you
# stop it, or decisions made meanwhile are lost.
#
# Writes divesafe-<UTC time>.dump (a pg_dump custom-format archive of the whole database, human
# decisions included) and divesafe-<UTC time>.manifest. The facts in the manifest are read from the
# live database, once before and once after the dump (if they differ, a write happened meanwhile
# and it tries again). Before it reports success the archive is loaded into a throwaway server in a
# container with no network and no secrets, and what is found there must equal the live facts. A
# backup that cannot be restored, or is short, is reported now and not on the day it is needed.
# That check catches accidents; it cannot stop an archive from making the throwaway server print
# whatever it likes.
#
# The archive holds assessments, evidence and named human decisions: treat it like the database.
# Encrypting it and moving it off this machine are yours (see the document). It never prints the
# database password, and it contains no role or password. It does not use the database superuser.
set -eu
unset CDPATH

here="$(cd "$(dirname "$0")" && pwd)"
. "$here/backup-common.sh"

dir="${1:?usage: backup.sh <directory>}"
case "$dir" in -*) dir="./$dir" ;; esac
umask 077
set -C
[ -d "$dir" ] || mkdir -m 0700 "$dir"
dir="$(cd "$dir" && pwd -P)"
mode="$(ls -ld "$dir" | cut -d' ' -f1)"
aces=$(($(ls -lde "$dir" 2>/dev/null | wc -l)))
mine="$(find "$dir" -maxdepth 0 -type d -perm 0700 -user "$(id -u)")"
case "$mode" in *+) mine="" ;; esac
case "$(uname -s)" in Darwin) [ "$aces" -eq 1 ] || mine="" ;; esac
if [ -z "$mine" ]; then
  echo "refusing: $dir must be a directory of yours with mode 0700 and no ACL" >&2
  exit 1
fi

repo="$(cd "$here/.." && pwd -P)"
case "$dir/" in
  "$repo"/*)
    echo "refusing: $dir is inside the repository; keep backups outside it" >&2
    exit 1
    ;;
esac

db="$(one_container db)"
single_database
echo "backing up project '$project' (volume ${project}_db-data)" >&2
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
base="$dir/divesafe-$stamp"
lock="$dir/.backup.lock"
if ! mkdir "$lock" 2>/dev/null; then
  echo "another backup is running, or a killed one left $lock (remove it if none is running)" >&2
  exit 1
fi

cleanup() {
  rm -f "$base.dump.part" "$base.manifest.part"
  rmdir "$lock" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 1' INT TERM HUP

if [ -e "$base.dump" ] || [ -e "$base.manifest" ]; then
  echo "already exists: $base.dump (wait a second and retry)" >&2
  exit 1
fi

attempt=0
while :; do
  attempt=$((attempt + 1))
  before="$(read_live)" || before=""
  rm -f "$base.dump.part"
  in_db_owner pg_dump -h db -U divesafe_owner -d divesafe --format=custom --no-password >"$base.dump.part"
  [ -s "$base.dump.part" ] || { echo "the dump is empty" >&2; exit 1; }
  after="$(read_live)" || after=""
  if [ -n "$before" ] && [ "$before" = "$after" ]; then
    break
  fi
  if [ "$attempt" -ge 3 ]; then
    echo "the database kept changing, or its facts could not be read, during the dump; try again" >&2
    exit 1
  fi
done

if ! shape_ok "$before"; then
  echo "a guard trigger is missing or disabled, a table is not a table, or the database has a policy, a default privilege, a stored setting, a rule, another schema or an owner with too many rights; not kept" >&2
  exit 1
fi
scratch="$(read_scratch "$base.dump.part")" || scratch=""
if [ "$scratch" != "$before" ]; then
  echo "the archive, restored in a scratch server, does NOT match the live database (or could not be loaded); not kept" >&2
  exit 1
fi

{
  echo "format=1"
  echo "created_utc=$stamp"
  echo "dump_file=$(basename "$base.dump")"
  echo "dump_sha256=$(sha256_of "$base.dump.part")"
  printf '%s\n' "$before"
} >"$base.manifest.part"

mv "$base.dump.part" "$base.dump"
mv "$base.manifest.part" "$base.manifest"

echo "backup written: $base.dump" >&2
echo "scratch restore equals the live database: $(printf '%s\n' "$before" | grep -E '^(assessments|decided)=' | tr '\n' ' ')" >&2
