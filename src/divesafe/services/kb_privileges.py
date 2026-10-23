"""The knowledge loader's privilege self-check (ADR 0022 decision 11, conditions 1 and 10).

`loader_privilege_problems` only reports. `load` and `approve` refuse on any problem; `withdraw`
reports after its commit. Each label is fixed text and never contains a role name or a value.
It builds on the app role's check (`PostgresAssessmentRepository._privilege_problems`) and does not
claim to match it. All checks run in ONE statement, so the check either completes or raises.
"""

from __future__ import annotations

import contextlib
from typing import Any

import psycopg
from psycopg import sql

from divesafe.config.roles import RESERVED_ROLE_NAMES
from divesafe.services.kb_common import require_limit
from divesafe.services.migrations import KNOWLEDGE_TABLES
from divesafe.services.postgres import _LOADER_EVENT_COLUMNS, _PARAMETER_PRIVILEGES_FROM

KB_FUNCTIONS = (
    "kb_no_update_delete",
    "kb_no_truncate",
    "kb_documents_insert",
    "kb_chunks_insert",
    "kb_status_events_insert",
)
KB_ORDER_SEQUENCE = "public.kb_status_event_order_seq"
OTHER_TABLES = ("assessments", "pending_runs", "run_failure_counts", "schema_migrations")
_EVENTS_TABLE = "kb_status_events"
_ALL_TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER"


class SelfCheckIncomplete(Exception):
    """The check could not finish (error, timeout, lost connection). Carries no detail."""


class SelfCheckConnectionLost(SelfCheckIncomplete):
    """The role check failed with a connection-class error. Carries no detail."""


def _lit(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _member_of(condition: str) -> str:
    return (
        f"EXISTS (SELECT 1 FROM pg_roles r WHERE ({condition}) "
        "AND pg_has_role(current_user, r.oid, 'MEMBER'))"
    )


def running_as_privileged_role(conn: psycopg.Connection[Any], *, statement_timeout_ms: int) -> bool:
    """True when the session is, or is a member of, the bootstrap, owner or app role, or owns the
    status-event table. Unknown counts as true (fails closed). Own read-only transaction, own
    statement_timeout; raises `SelfCheckIncomplete` when it cannot finish. The app role's name is
    not known to the loader (open question), so the reserved names stand in for it."""
    try:
        conn.read_only = True
        conn.execute("SET LOCAL search_path = pg_catalog, pg_temp")
        conn.execute(
            sql.SQL("SET LOCAL statement_timeout = {}").format(
                sql.Literal(require_limit(statement_timeout_ms))
            )
        )
        row = conn.execute(
            sql.SQL(
                "SELECT {member} OR COALESCE((SELECT pg_has_role(current_user, c.relowner, "
                "'MEMBER') FROM pg_class c WHERE c.oid = to_regclass({table})), true)"
            ).format(
                member=sql.SQL(_member_of(f"r.rolname IN ({_lit(RESERVED_ROLE_NAMES)})")),
                table=sql.Literal(f"public.{_EVENTS_TABLE}"),
            )
        ).fetchone()
        conn.rollback()
    except psycopg.OperationalError as exc:
        _end(conn)
        if exc.sqlstate != "57014":  # a statement timeout is not a lost connection
            raise SelfCheckConnectionLost("the role check lost the connection") from None
        raise SelfCheckIncomplete("the role check could not finish") from None
    except Exception:
        _end(conn)
        raise SelfCheckIncomplete("the role check could not finish") from None
    return row is None or row[0] is not False


def _checks() -> list[tuple[str, str]]:
    kb = _lit(KNOWLEDGE_TABLES)
    fns = _lit(KB_FUNCTIONS)
    allowed = _lit(tuple(_LOADER_EVENT_COLUMNS))
    attribute_scan = (
        "EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
        "AND a.attnum > 0 AND NOT a.attisdropped AND {where})"
    )
    checks: list[tuple[str, str]] = [
        (
            _member_of(f"r.rolname IN ({_lit(RESERVED_ROLE_NAMES)})"),
            "is, or is a member of, the owner, app or bootstrap role",
        ),
        (
            "COALESCE((SELECT pg_has_role(current_user, c.relowner, 'MEMBER') FROM pg_class c "
            f"WHERE c.oid = to_regclass('public.{_EVENTS_TABLE}')), true)",
            "owns a knowledge table, or the table is missing",
        ),
        (
            "EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = 'public' AND c.relname IN ({kb}) "
            "AND pg_has_role(current_user, c.relowner, 'MEMBER'))",
            "owns a knowledge table",
        ),
        (
            "(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = 'public' AND c.relname IN ({kb})) <> {len(KNOWLEDGE_TABLES)}",
            "a knowledge table is missing",
        ),
        (
            "COALESCE((SELECT pg_has_role(current_user, nspowner, 'MEMBER') FROM pg_namespace "
            "WHERE nspname = 'public'), true)",
            "owns the public schema",
        ),
        (
            "COALESCE((SELECT pg_has_role(current_user, datdba, 'MEMBER') FROM pg_database "
            "WHERE datname = current_database()), true)",
            "owns the database",
        ),
        (
            "COALESCE(has_schema_privilege('public', 'CREATE'), true)",
            "can create objects in the public schema",
        ),
        (
            "COALESCE(has_database_privilege(current_database(), 'CREATE'), true)",
            "can create schemas in the database",
        ),
        (_member_of("r.rolsuper"), "is a superuser or a member of one"),
        (
            "EXISTS (SELECT 1 FROM pg_roles r WHERE r.rolname <> current_user "
            "AND pg_has_role(current_user, r.oid, 'MEMBER'))",
            "is a member of a role other than itself",
        ),
        (
            _member_of("r.rolreplication"),
            "can replicate, or is a member of a role that can",
        ),
        (_member_of("r.rolcreaterole"), "can create roles, or is a member of a role that can"),
        (_member_of("r.rolcreatedb"), "can create databases, or is a member of a role that can"),
        (
            _member_of("r.rolbypassrls"),
            "bypasses row-level security, or is a member of a role that does",
        ),
        (
            _member_of(
                "r.rolname IN ('pg_execute_server_program', 'pg_write_server_files', "
                "'pg_read_server_files')"
            ),
            "is a member of a role that can run programs or read or write server files",
        ),
        (
            "COALESCE(has_database_privilege(current_database(), 'TEMPORARY'), true)",
            "has TEMPORARY on the database (directly or through PUBLIC)",
        ),
        (
            "EXISTS (SELECT 1 FROM pg_roles r WHERE r.rolname <> current_user "
            "AND pg_has_role(current_user, r.oid, 'MEMBER') "
            "AND has_database_privilege(r.oid, current_database(), 'TEMPORARY') IS NOT FALSE)",
            "can reach TEMPORARY through a role it is a member of",
        ),
        (
            "current_setting('session_replication_role') <> 'origin'",
            "runs with session_replication_role other than origin",
        ),
        (
            "EXISTS (SELECT 1 FROM pg_db_role_setting s, unnest(s.setconfig) AS cfg "
            "WHERE (s.setrole = 0 OR s.setrole IN (SELECT r.oid FROM pg_roles r "
            "WHERE pg_has_role(current_user, r.oid, 'MEMBER'))) "
            "AND cfg ILIKE 'session_replication_role=%')",
            "has a stored session_replication_role setting",
        ),
        (
            "EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = 'public' AND c.relname IN ({kb}) "
            "AND has_table_privilege(c.oid, 'UPDATE, DELETE, TRUNCATE, TRIGGER, REFERENCES'))",
            "has UPDATE, DELETE, TRUNCATE, TRIGGER or REFERENCES on a knowledge table",
        ),
        (
            attribute_scan.format(
                where=f"c.relname IN ({kb}) AND has_column_privilege(c.oid, a.attnum, "
                "'UPDATE, REFERENCES')"
            ),
            "has a column UPDATE or REFERENCES privilege on a knowledge table",
        ),
        (
            attribute_scan.format(
                where=f"c.relname = '{_EVENTS_TABLE}' AND a.attname NOT IN ({allowed}) "
                "AND has_column_privilege(c.oid, a.attnum, 'INSERT')"
            ),
            "can INSERT a status-event column beyond its grant (ordering key or recorded-at)",
        ),
        (
            f"COALESCE(has_sequence_privilege(to_regclass('{KB_ORDER_SEQUENCE}'), "
            "'USAGE, SELECT, UPDATE'), false)",
            "has a privilege on the status-event ordering sequence",
        ),
        (
            "EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            f"WHERE n.nspname = 'public' AND p.proname IN ({fns}) "
            "AND has_function_privilege(p.oid, 'EXECUTE'))",
            "can EXECUTE a knowledge function",
        ),
        (
            "EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            f"WHERE n.nspname = 'public' AND p.proname IN ({fns}) "
            "AND pg_has_role(current_user, p.proowner, 'MEMBER'))",
            "owns a knowledge function",
        ),
    ]
    for table in OTHER_TABLES:
        checks.append(
            (
                f"(to_regclass('public.{table}') IS NOT NULL AND ("
                f"has_table_privilege(to_regclass('public.{table}'), '{_ALL_TABLE_PRIVILEGES}') "
                f"OR has_any_column_privilege(to_regclass('public.{table}'), "
                "'SELECT, INSERT, UPDATE, REFERENCES')))",
                f"has a privilege on {table}",
            )
        )
    return checks


def loader_privilege_problems(
    conn: psycopg.Connection[Any], *, statement_timeout_ms: int
) -> list[str]:
    """Problems with the connected role, as fixed labels. Runs in its OWN read-only transaction
    with its OWN statement_timeout and always ends it. Raises `SelfCheckIncomplete` when it cannot
    finish. The connection must not be inside a transaction."""
    try:
        conn.read_only = True
        conn.execute("SET LOCAL search_path = pg_catalog, pg_temp")
        conn.execute(
            sql.SQL("SET LOCAL statement_timeout = {}").format(
                sql.Literal(require_limit(statement_timeout_ms))
            )
        )
        checks = _checks()
        version = conn.execute("SELECT current_setting('server_version_num')::int").fetchone()
        if version is not None and version[0] >= _PARAMETER_PRIVILEGES_FROM:
            checks.append(
                (
                    "COALESCE(has_parameter_privilege('session_replication_role', 'SET'), true)",
                    "may set session_replication_role",
                )
            )
        row = conn.execute("SELECT " + ", ".join(expr for expr, _ in checks)).fetchone()
        if row is None or len(row) != len(checks):
            raise SelfCheckIncomplete("the self-check returned no result")
        problems = [
            label for (_, label), flag in zip(checks, row, strict=True) if flag is not False
        ]
        conn.rollback()
        return problems
    except SelfCheckIncomplete:
        _end(conn)
        raise
    except Exception:
        _end(conn)
        raise SelfCheckIncomplete("the self-check could not finish") from None


def _end(conn: psycopg.Connection[Any]) -> None:
    with contextlib.suppress(Exception):
        conn.rollback()
