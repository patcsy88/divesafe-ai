#!/bin/sh
# Runs once, when the database volume is first created (docker-compose.production.yml, ADR 0024).
# The bootstrap role (POSTGRES_USER) is a PostgreSQL superuser and is reachable only through the
# local socket (docker/db/pg_hba.conf). This script creates the two roles that connect over the
# network:
#   divesafe_owner  owns the database and creates the schema (db-init). Not a superuser.
#   divesafe_app    what the API uses: no create rights and no privilege on any table yet.
#                   db_init grants SELECT/INSERT/UPDATE (and DELETE on pending_runs only).
# The names must match docker/make-secrets.sh and DIVESAFE_APP_DB_ROLE in the compose file.
# Changing a password secret later does not change the role: run ALTER ROLE yourself.
# Passwords are read from the files into psql variables through the environment, never put on a
# command line, and the statements that carry them are kept out of the server log.
set -eu

OWNER_PW="$(cat /run/secrets/db_owner_password)" APP_PW="$(cat /run/secrets/db_app_password)" \
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=db_name="$POSTGRES_DB" <<'SQL'
\getenv owner_password OWNER_PW
\getenv app_password APP_PW
SET log_min_error_statement = panic;
CREATE ROLE divesafe_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'owner_password';
CREATE ROLE divesafe_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'app_password';
ALTER DATABASE :"db_name" OWNER TO divesafe_owner;
REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db_name" TO divesafe_owner, divesafe_app;
-- PostgreSQL lets every role connect to these by default. Neither new role needs them.
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;
SQL
