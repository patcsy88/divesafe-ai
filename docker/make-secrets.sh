#!/bin/sh
# Creates the database secrets for docker-compose.production.yml (ADR 0024) in a directory that
# only you can enter. It never overwrites a file and never prints a secret.
#
#   docker/make-secrets.sh docker/secrets
#
# It does NOT create the two secrets only you can supply. Add them yourself, without putting them
# in your shell history (read -rs), then check the permissions:
#   divesafe_llm_api_key         the key for the model host
#   divesafe_api_key_hashes      the JSON table of key hashes (see .env.example)
set -eu

dir="${1:?usage: make-secrets.sh <directory>}"
umask 077
mkdir -p "$dir"
chmod 0700 "$dir"

# Files are readable by every user inside the container (0444) because the container runs as a
# different user from you. The directory is what keeps other users on this host out.
write_new() {
  target="$dir/$1"
  if [ -e "$target" ]; then
    echo "kept existing: $target" >&2
    return 1
  fi
  cat >"$target"
  chmod 0444 "$target"
  echo "created: $target" >&2
}

random() { openssl rand -hex 24; }

bootstrap_password="$(random)"
owner_password="$(random)"
app_password="$(random)"

printf '%s' "$bootstrap_password" | write_new db_bootstrap_password || true

if printf '%s' "$owner_password" | write_new db_owner_password; then
  printf '%s' "postgresql://divesafe_owner:${owner_password}@db:5432/divesafe?sslmode=require&channel_binding=require" |
    write_new divesafe_admin_database_url || true
fi
if printf '%s' "$app_password" | write_new db_app_password; then
  printf '%s' "postgresql://divesafe_app:${app_password}@db:5432/divesafe?sslmode=require&channel_binding=require" |
    write_new divesafe_database_url || true
fi

# An empty file means "not set" (the MET connector is then not used).
[ -e "$dir/divesafe_met_api_token" ] || { : | write_new divesafe_met_api_token; }

for needed in divesafe_llm_api_key divesafe_api_key_hashes; do
  [ -e "$dir/$needed" ] || echo "still to add by hand: $dir/$needed" >&2
done
