# ADR 0012: PostgreSQL assessment repository

- Status: Accepted
- Date: 2026-10-10

## Decision

- `PostgresAssessmentRepository` (psycopg 3, new dependency; LGPL-3.0; the `binary` extra bundles
  libpq, which lags distro security patches, so a production image should prefer `psycopg[c]`)
  implements `AssessmentRepository` and passes the same contract tests as the in-memory adapter.
- One JSONB document per record in `assessments (id, record, created_at)`; the domain model stays
  the single schema.
- `replace` is compare-and-set in a transaction with `SELECT ... FOR UPDATE`; it checks the row
  count and reads the row back. `create` reads back too and refuses (rolls back) a record that
  would not read back identical (NaN/Infinity, NUL characters, numbers JSONB would change).
- **Guard in the database**, as triggers: no delete; no truncate; the id and `created_at` never
  change; only a first, well-formed `human_decision` and a first `actual_conditions` (which need a
  decision) may be appended; a new row cannot arrive already decided. Written in the same terms as
  `check_successor`; both are tested against the same cases.
- **Roles.** Schema and triggers are installed by `python -m divesafe.services.db_init` with an
  owner DSN (`DIVESAFE_ADMIN_DATABASE_URL`), which also grants the application role only SELECT,
  INSERT, UPDATE. The running app never creates or changes DDL: at start-up it verifies the table
  and all guard triggers exist and are enabled, and in production also that its role does not own
  the table, is not a superuser and cannot DELETE, TRUNCATE or alter triggers. Otherwise it
  refuses to start.
- **Connections.** Connect, statement and lock timeouts. Production refuses a DSN whose `sslmode`
  is not `require`, `verify-ca` or `verify-full`. Driver and validation errors become
  `RepositoryUnavailableError` (HTTP 503, "nothing was recorded"), `RepositoryIntegrityError` or
  `UnstorableRecordError` (HTTP 422), raised `from None` so no DSN or record text reaches logs.
  `create` runs in a worker thread so a slow database cannot stall the event loop.
- NUL characters are refused at the decision, actual-conditions and connector boundaries.
- Selected by `DIVESAFE_DATABASE_URL`; unset means in-memory (refused in production).

## Tests

`DIVESAFE_TEST_DATABASE_URL` (a database named `*_test`; the fixture refuses anything else)
enables the PostgreSQL contract tests and `tests/integration/test_postgres_guard.py`, which
attack the guard with raw SQL and use a real non-owner role. Run against `pgvector/pgvector:pg16`.
Without it these tests are skipped, so a plain `pytest` does not prove the adapter; CI must set it.

## Not solved

- Migrations: see ADR 0015 (forward-only runner, checksums, start-up version check).
- **A superuser or the table owner can still disable the guard.** The runtime checks above catch a
  misconfigured application role, not a compromised owner.
- **The trigger checks the shape of a decision, not its authenticity.** A role with UPDATE can
  append a well-formed forged first decision; it is then irreversible. Tamper evidence (record
  hashing, a decision-only function) is not built.
- **Float values in free-form evidence `value`** that JSONB would re-type (for example 1e22) are
  refused rather than stored; such data cannot be recorded.
- Stored JSON from an older model version must still validate; no upcasting yet (a failing read is
  a 500 with no content logged).
- No connection pool, backups, or replication. An ambiguous commit failure (network drop after the
  server committed) is reported as an error although the write may exist; a retry then conflicts.
- Roles exist (ADR 0013) but there is no per-assessment ownership: any viewer reads any assessment.
