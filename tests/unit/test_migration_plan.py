"""Migration planning is pure: it decides what to run and refuses inconsistent history."""

from __future__ import annotations

import pytest

from divesafe.services.migrations import MIGRATIONS, Migration, MigrationError, plan, validate_known

M1 = Migration(1, "one", "SELECT 1")
M2 = Migration(2, "two", "SELECT 2")
KNOWN = (M1, M2)


def _row(m: Migration) -> tuple[int, str, str]:
    return (m.version, m.name, m.checksum)


def test_an_empty_database_runs_everything_in_order() -> None:
    assert plan([], KNOWN) == [M1, M2]


def test_a_partly_migrated_database_runs_only_the_rest() -> None:
    assert plan([_row(M1)], KNOWN) == [M2]
    assert plan([_row(M1), _row(M2)], KNOWN) == []


def test_an_edited_migration_is_refused() -> None:
    edited = Migration(1, "one", "SELECT 'changed'")
    with pytest.raises(MigrationError, match="changed after it was applied"):
        plan([_row(edited)], KNOWN)


def test_a_renamed_migration_is_refused() -> None:
    with pytest.raises(MigrationError, match="changed after it was applied"):
        plan([(1, "other name", M1.checksum)], KNOWN)


def test_a_database_ahead_of_the_code_is_refused() -> None:
    m3 = Migration(3, "three", "SELECT 3")
    with pytest.raises(MigrationError, match="ahead of this code"):
        plan([_row(M1), _row(M2), _row(m3)], KNOWN)


def test_a_gap_in_the_applied_history_is_refused() -> None:
    with pytest.raises(MigrationError, match="gap or an unknown version"):
        plan([_row(M2)], KNOWN)


@pytest.mark.parametrize(
    "known",
    [
        (Migration(2, "x", "SELECT 1"),),
        (M1, Migration(3, "x", "SELECT 1")),
        (M1, Migration(2, "  ", "SELECT 1")),
        (M1, Migration(2, "x", "  ")),
    ],
)
def test_a_malformed_migration_list_is_refused(known: tuple[Migration, ...]) -> None:
    with pytest.raises(MigrationError):
        validate_known(known)


def test_the_shipped_migrations_are_well_formed() -> None:
    validate_known(MIGRATIONS)
    assert MIGRATIONS[0].version == 1
