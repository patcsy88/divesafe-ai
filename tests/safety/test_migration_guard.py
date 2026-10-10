"""Migrations may not quietly weaken the audit guard (ADR 0015). Database-free."""

from __future__ import annotations

import pytest

from divesafe.services.migrations import (
    FORBIDDEN_IN_LATER_MIGRATIONS,
    GUARD_TRIGGERS,
    MIGRATIONS,
    Migration,
    MigrationError,
    guarded,
)

pytestmark = pytest.mark.safety


def test_every_guard_trigger_is_created_by_migration_one() -> None:
    sql = MIGRATIONS[0].sql
    for name in GUARD_TRIGGERS:
        assert f"CREATE TRIGGER {name}" in sql


def test_the_first_migration_is_pinned() -> None:
    assert MIGRATIONS[0].checksum == (
        "63be72fc71900c1d45852ef73496a6374d16ce9fc65748bc01caf894ccb00934"
    )


def test_no_shipped_later_migration_touches_the_guard() -> None:
    guarded(MIGRATIONS)


@pytest.mark.parametrize("word", FORBIDDEN_IN_LATER_MIGRATIONS)
def test_a_later_migration_that_touches_the_guard_is_refused(word: str) -> None:
    evil = Migration(2, "evil", f"-- harmless looking\nSELECT 1; {word.upper()} x;")
    with pytest.raises(MigrationError, match="touches the audit guard"):
        guarded((MIGRATIONS[0], evil))


def test_the_replace_function_attack_is_refused() -> None:
    evil = Migration(
        2,
        "evil",
        "CREATE OR REPLACE FUNCTION public.assessments_append_only() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
    )
    with pytest.raises(MigrationError):
        guarded((MIGRATIONS[0], evil))
