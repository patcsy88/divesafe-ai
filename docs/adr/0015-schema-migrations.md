# ADR 0015: Forward-only schema migrations

Status: accepted

## Context

`db_init` ran one idempotent `CREATE ... IF NOT EXISTS`. A later change to the table or its
guard triggers could not be applied to an existing database, and nothing told the application it
was running against an older schema (ADR 0012).

## Decision

A small in-house runner, `divesafe.services.migrations`, with no new dependency.

- A migration is a version (1, 2, 3, no gaps), a name and SQL, kept in code (`MIGRATIONS`).
- `db_init` (owner role) applies pending migrations in one transaction under a PostgreSQL
  advisory lock, so concurrent runs are safe and a failing migration leaves nothing behind.
- Each applied migration is recorded in `schema_migrations` with a SHA-256 checksum of its SQL.
  Editing, renaming or removing an applied migration is refused. A database that is ahead of the
  code, or has a gap, is refused.
- The application role gets `SELECT` on `schema_migrations` only. At start-up `verify` reports a
  database that is behind, ahead or inconsistent, and the application refuses to start.
- Forward-only: there is no downgrade. Fix a bad migration with a new one.
- The existing schema is migration 1. Its checksum is pinned in a test.
- Existing databases need no manual step: migration 1 is idempotent (`IF NOT EXISTS`,
  `CREATE OR REPLACE`, drop-and-create triggers), so running `db_init` records it.

## Protections around the audit guard

- Migration 1 holds the guard SQL moved verbatim from `postgres.py` (checked identical to the
  previous version; its checksum is pinned in a safety test).
- A later migration may not mention the guard functions or triggers, or `DISABLE TRIGGER`,
  `DROP TRIGGER`, `ALTER FUNCTION`, `DROP FUNCTION`, `DISABLE ROW LEVEL` or
  `session_replication_role` (`FORBIDDEN_IN_LATER_MIGRATIONS`). This is a word scan, not a SQL
  parser; it stops accidents and obvious attacks, and review is still the control.
- After migrating, and before commit, the runner checks the three guard triggers are enabled and
  rolls back if not.
- `migrate` refuses to run as the application role and sets a lock timeout.
- `install_schema` grants the application role `SELECT` on `schema_migrations` and revokes every
  write privilege. Start-up in production reports a role that can write the history.
- Role names `public` and `pg_*` are refused.

## Rules for writing a migration

- Never edit one that has been released. Add the next version.
- Keep every guard intact: a migration that changes the table must keep the append-only triggers
  enabled, and `GUARD_TRIGGERS` must list any new one.
- A change to the stored record's shape needs its own review (risk-reviewer) because the triggers
  read fields of the JSON record.

## Not solved

- A migration that rewrites stored records trips the append-only triggers. Such a change needs a
  separate design and risk-reviewer sign-off; do not disable the triggers.
- Start-up checks that the triggers exist and are enabled, but not their function bodies. A
  function swap is blocked by the word scan and review only.
- A pre-existing `assessments` table of a different shape is recorded as migration 1 without a
  shape check.
- Verification runs at start-up only, so later tampering with the history is noticed at the next
  restart. A database ahead of the code makes older replicas refuse to restart after a rollout.
- PostgreSQL 15 or later is assumed (the `public` schema no longer lets everyone create tables).
- Changes to `migrations.py` should need `risk-reviewer` sign-off (a CODEOWNERS rule is not set up).
- Migrations run as the owner role, which is trusted; the trigger does not stop the owner or a
  superuser (ADR 0012).
- No dry run, no downgrade, and no check that the SQL is safe to run on a large table.
- A migration that runs long holds locks; plan a window for those.
- The migration list and the checksum live in the same code, so someone who can change both can
  rewrite history; the pinned test and review are the control.
- The runner is not tested against several PostgreSQL versions (only 16).
- CI must set `DIVESAFE_TEST_DATABASE_URL`, or the PostgreSQL tests skip.
