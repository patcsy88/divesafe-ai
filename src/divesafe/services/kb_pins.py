"""Dev-only helper: print the pins for `migrations.KNOWLEDGE_FUNCTION_SHA256` and
`KNOWLEDGE_TRIGGER_TGTYPE` (ADR 0022 condition 9).

    DIVESAFE_PIN_DSN='postgresql://...' DIVESAFE_PIN_APPLY=1 python -m divesafe.services.kb_pins

Pins come ONLY from a fresh, empty scratch database with DIVESAFE_PIN_APPLY=1, are confirmed by
the live pin test before they are pasted into `migrations.py`, and never come from the database
being checked. APPLY applies the migrations in one transaction, reads the values and ROLLS BACK,
and is refused unless `public.schema_migrations` does not exist (an empty scratch database only).
Without APPLY it reads a database already at version 4, read-only (for inspection, not for pins).
Output: `postgresql_major <n>`, then `<function> <sha256 hex>` per function, then
`tgtype <trigger> <integer>` per trigger. The DSN comes from the environment, not argv, and is
never printed. Nothing here is used at run time.
"""

from __future__ import annotations

import os
import sys

import psycopg

from divesafe.services.migrations import (
    MIGRATIONS,
    knowledge_function_hashes,
    knowledge_trigger_types,
)

_OPTIONS = "-c lock_timeout=5000 -c statement_timeout=30000"


def main() -> int:
    dsn = os.environ.get("DIVESAFE_PIN_DSN")
    if not dsn:
        sys.stderr.write("set DIVESAFE_PIN_DSN\n")
        return 2
    apply = os.environ.get("DIVESAFE_PIN_APPLY") == "1"
    try:
        with psycopg.connect(dsn, connect_timeout=5, options=_OPTIONS) as conn:
            if apply:
                row = conn.execute("SELECT pg_catalog.to_regclass('public.schema_migrations')")
                if row.fetchone() != (None,):
                    conn.rollback()
                    sys.stderr.write("refused: APPLY needs an empty scratch database\n")
                    return 3
                for m in MIGRATIONS:
                    conn.execute(m.sql)
            else:
                conn.read_only = True
            major, hashes = knowledge_function_hashes(conn)
            types = knowledge_trigger_types(conn)
            conn.rollback()
    except psycopg.Error:
        sys.stderr.write("could not read the database\n")
        return 4
    sys.stdout.write(f"postgresql_major {major}\n")
    for name, digest in hashes.items():
        sys.stdout.write(f"{name} {digest}\n")
    for name, value in types.items():
        sys.stdout.write(f"tgtype {name} {value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
