"""The A7 hand-run seed (tests/hand_run/a7_seed_knowledge.sql) is accepted by the real knowledge
triggers and leaves the intended statuses; a second run is refused and writes nothing. Runs as
the owner on its OWN scratch database (named *_test, force-dropped). Needs
DIVESAFE_TEST_DATABASE_URL."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from tests.unit.test_repository_contract import TEST_DSN as DSN

from divesafe.services.postgres import install_schema

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DSN is None, reason="DIVESAFE_TEST_DATABASE_URL is not set"),
]

SEED = Path(__file__).parents[1] / "hand_run" / "a7_seed_knowledge.sql"


def _dsn_for(database: str) -> str:
    assert DSN is not None
    parts = psycopg.conninfo.conninfo_to_dict(DSN)
    parts["dbname"] = database
    return psycopg.conninfo.make_conninfo(**parts)  # type: ignore[arg-type]


@pytest.fixture
def scratch() -> Iterator[str]:
    assert DSN is not None
    name = str(psycopg.conninfo.conninfo_to_dict(DSN).get("dbname", ""))
    if not name.endswith("_test"):
        raise RuntimeError("DIVESAFE_TEST_DATABASE_URL must point at a database named *_test")
    database = f"divesafe_a7seed_{uuid.uuid4().hex[:10]}_test"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{database}"')  # type: ignore[call-overload]
    try:
        dsn = _dsn_for(database)
        install_schema(dsn)
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')  # type: ignore[call-overload]


def _seed_sql() -> str:
    """The file without psql meta-commands (`\\set`), which the server would not understand."""
    return "\n".join(line for line in SEED.read_text().splitlines() if not line.startswith("\\"))


def _state(dsn: str) -> list[tuple[str, list[str]]]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT document_id, array_agg(status ORDER BY event_order) "
            "FROM public.kb_status_events GROUP BY document_id ORDER BY document_id"
        ).fetchall()
    return [(str(r[0]), list(r[1])) for r in rows]


def test_the_seed_is_accepted_by_the_triggers_and_refuses_a_second_run(scratch: str) -> None:
    with psycopg.connect(scratch, autocommit=True) as conn:
        conn.execute(_seed_sql())  # type: ignore[call-overload]
    expected = [
        ("a7-approved", ["DRAFT", "APPROVED"]),
        ("a7-draft", ["DRAFT"]),
        ("a7-withdrawn", ["DRAFT", "APPROVED", "WITHDRAWN"]),
    ]
    assert _state(scratch) == expected
    with psycopg.connect(scratch) as conn:
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM public.kb_documents),"
            " (SELECT count(*) FROM public.kb_chunks)"
        ).fetchone()
    assert counts == (3, 4)
    with psycopg.connect(scratch) as conn:
        texts = conn.execute(
            "SELECT title FROM public.kb_documents"
            " UNION ALL SELECT chunk_text FROM public.kb_chunks"
        ).fetchall()
    assert len(texts) == 7 and all(str(t[0]).startswith("SYNTHETIC A7 hand-run") for t in texts)
    with (
        psycopg.connect(scratch, autocommit=True) as conn,
        pytest.raises(psycopg.errors.RaiseException, match="already holds knowledge rows"),
    ):
        conn.execute(_seed_sql())  # type: ignore[call-overload]
    assert _state(scratch) == expected


def test_the_seed_never_goes_through_the_loader() -> None:
    """Code lines only (comments say what the files do not do)."""
    for path in (SEED, SEED.with_suffix(".sh")):
        code = [
            line
            for line in path.read_text().splitlines()
            if not line.lstrip().startswith(("--", "#"))
        ]
        joined = "\n".join(code)
        assert "kb_load" not in joined and "kb_withdraw" not in joined, path.name
        assert "PRODUCTION_LOAD_GATES_CLOSED" not in joined, path.name
    assert "SYNTHETIC" in SEED.read_text()
