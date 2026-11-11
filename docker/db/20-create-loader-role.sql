-- Creates the knowledge loader role (ADR 0024 Amendment 1, A1). Safe to re-run on an existing
-- volume: an existing, clean role is left as it is. It is NOT mounted into the init directory; the
-- bootstrap superuser runs it through the standard input of `docker compose exec -T` (see
-- docs/knowledge-loader.md for the exact command).
-- The password is read by the server from the mounted secret file. It is never in this file, in a
-- psql variable or in an environment variable, and it is never selected: the generated statement
-- is run with \gexec. Errors print no statement text and no context, and nothing is logged.
-- The role name is a literal here and must equal the loader rule in pg_hba.conf.
\set ON_ERROR_STOP on
\set VERBOSITY terse
\set SHOW_CONTEXT never
SET log_statement = 'none';
SET log_min_duration_statement = -1;
SET log_min_error_statement = panic;
SET search_path = pg_catalog, pg_temp;
SELECT 'divesafe_loader' IN ('divesafe_bootstrap', 'divesafe_owner', 'divesafe_app') AS loader_name_reserved \gset
\if :loader_name_reserved
\warn 'refusing: the loader role name is reserved'
SELECT 'refusing: the loader role name is reserved'::integer;
\endif
SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'divesafe_loader') AS loader_exists \gset
\if :loader_exists
SELECT (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls OR NOT r.rolcanlogin OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid OR m.roleid = r.oid) OR EXISTS (SELECT 1 FROM pg_catalog.pg_db_role_setting s WHERE s.setrole = r.oid)) AS loader_role_unsafe FROM pg_catalog.pg_roles r WHERE r.rolname = 'divesafe_loader' \gset
\if :loader_role_unsafe
\warn 'refusing: divesafe_loader exists with an attribute, a membership or a stored setting; it is not repaired'
SELECT 'refusing: the existing loader role is not clean'::integer;
\endif
\else
SELECT pg_catalog.pg_read_file('/run/secrets/db_loader_password') ~ '^[0-9a-f]{48}$' AS loader_password_ok \gset
\if :loader_password_ok
SELECT pg_catalog.format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', 'divesafe_loader', pg_catalog.pg_read_file('/run/secrets/db_loader_password')) \gexec
\else
\warn 'refusing: db_loader_password is not 48 lower-case hex characters with no newline'
SELECT 'refusing: the loader password file is not in the expected form'::integer;
\endif
\endif
GRANT CONNECT ON DATABASE divesafe TO divesafe_loader;
