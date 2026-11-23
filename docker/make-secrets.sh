#!/bin/sh
# Creates the database, tracing and web UI secrets for docker-compose.production.yml (ADR 0024,
# ADR 0017) in a directory that only you can enter. It never overwrites a file and never prints
# a secret.
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
loader_password="$(random)"

printf '%s' "$bootstrap_password" | write_new db_bootstrap_password || true

if printf '%s' "$owner_password" | write_new db_owner_password; then
  printf '%s' "postgresql://divesafe_owner:${owner_password}@db:5432/divesafe?sslmode=require&channel_binding=require" |
    write_new divesafe_admin_database_url || true
fi
if printf '%s' "$app_password" | write_new db_app_password; then
  printf '%s' "postgresql://divesafe_app:${app_password}@db:5432/divesafe?sslmode=require&channel_binding=require" |
    write_new divesafe_database_url || true
fi
# The knowledge loader (ADR 0024 Amendment 1, A1). The role is created by
# docker/db/20-create-loader-role.sql, which requires exactly these 48 hex characters with no
# newline. connect_timeout=5 is the owner-approved ceiling the loader's settings accept.
if printf '%s' "$loader_password" | write_new db_loader_password; then
  printf '%s' "postgresql://divesafe_loader:${loader_password}@db:5432/divesafe?sslmode=require&channel_binding=require&connect_timeout=5" |
    write_new divesafe_loader_database_url || true
fi

# An empty file means "not set" (the MET connector is then not used).
[ -e "$dir/divesafe_met_api_token" ] || { : | write_new divesafe_met_api_token; }
# tides.today (ADR 0029): empty means "not set"; the connector also needs
# DIVESAFE_TIDES_TODAY_ENABLED (default false; the provider's permission covers only the
# owner's deployment, ADR 0029 status).
[ -e "$dir/divesafe_tides_today_api_key" ] || { : | write_new divesafe_tides_today_api_key; }
# The Langfuse production project's keys (ADR 0027 amendment). Empty means "not set"; with
# tracing on, start-up then refuses.
[ -e "$dir/divesafe_trace_public_key" ] || { : | write_new divesafe_trace_public_key; }
[ -e "$dir/divesafe_trace_secret_key" ] || { : | write_new divesafe_trace_secret_key; }
# The web UI (ADR 0017, owner #50a, #53a): random, never printed, one line with no newline, read
# by docker/web-start.cjs at start.
[ -e "$dir/divesafe_web_session_secret" ] ||
  openssl rand -hex 32 | tr -d '\n' | write_new divesafe_web_session_secret || true
[ -e "$dir/divesafe_web_actions_key" ] ||
  openssl rand -base64 32 | tr -d '\n' | write_new divesafe_web_actions_key || true
# sh has no pipefail, so a failed openssl would leave an empty file that is then always kept.
# One line, in the launcher's form (the launcher still checks again at start).
[ "$(grep -c '' "$dir/divesafe_web_session_secret")" -eq 1 ] &&
  grep -Eqx '[0-9a-f]{64}' "$dir/divesafe_web_session_secret" ||
  { echo "bad web session secret: delete $dir/divesafe_web_session_secret and rerun" >&2; exit 1; }
[ "$(grep -c '' "$dir/divesafe_web_actions_key")" -eq 1 ] &&
  grep -Eqx '[A-Za-z0-9+/]{43}=' "$dir/divesafe_web_actions_key" ||
  { echo "bad web actions key: delete $dir/divesafe_web_actions_key and rerun" >&2; exit 1; }

for needed in divesafe_llm_api_key divesafe_api_key_hashes; do
  [ -e "$dir/$needed" ] || echo "still to add by hand: $dir/$needed" >&2
done
