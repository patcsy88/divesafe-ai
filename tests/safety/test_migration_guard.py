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


def test_no_shipped_later_migration_touches_the_guard_except_the_pinned_reviewed_one() -> None:
    from divesafe.services.migrations import REVIEWED_GUARD_MIGRATIONS

    guarded(MIGRATIONS)
    unreviewed = [m for m in MIGRATIONS[1:] if m.version not in REVIEWED_GUARD_MIGRATIONS]
    assert [m.version for m in unreviewed] == [2]
    guarded((MIGRATIONS[0], *unreviewed))


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


# --- migration 3 makes the guard triggers ENABLE ALWAYS -------------------------------------------

REVIEWED_THREE = "7588a25481499fa29d84a995fa9a851c8056690ec7b3591f6c3cd1f59b306297"


def test_migration_three_is_pinned_and_is_the_only_reviewed_exception() -> None:
    from divesafe.services.migrations import REVIEWED_GUARD_MIGRATIONS

    assert MIGRATIONS[2].version == 3
    assert MIGRATIONS[2].checksum == REVIEWED_THREE
    assert REVIEWED_GUARD_MIGRATIONS == {3: REVIEWED_THREE}


def test_migration_three_only_sets_the_three_guard_triggers_to_enable_always() -> None:
    statements = [s.strip() for s in MIGRATIONS[2].sql.split(";") if s.strip()]
    assert statements == [
        f"ALTER TABLE public.assessments ENABLE ALWAYS TRIGGER {name}" for name in GUARD_TRIGGERS
    ]


def test_an_edited_copy_of_migration_three_is_refused_by_the_word_scan() -> None:
    edited = Migration(3, MIGRATIONS[2].name, MIGRATIONS[2].sql + "\n-- edited")
    with pytest.raises(MigrationError, match="touches the audit guard"):
        guarded((MIGRATIONS[0], MIGRATIONS[1], edited))


def test_the_exception_is_by_version_and_text_not_by_name() -> None:
    same_text_other_version = Migration(4, MIGRATIONS[2].name, MIGRATIONS[2].sql)
    with pytest.raises(MigrationError, match="touches the audit guard"):
        guarded((*MIGRATIONS, same_text_other_version))
    weakened = Migration(
        3,
        MIGRATIONS[2].name,
        "ALTER TABLE public.assessments ENABLE TRIGGER assessments_insert_guard",
    )
    with pytest.raises(MigrationError, match="touches the audit guard"):
        guarded((MIGRATIONS[0], MIGRATIONS[1], weakened))


@pytest.mark.parametrize("word", FORBIDDEN_IN_LATER_MIGRATIONS)
def test_a_fourth_migration_cannot_use_the_exception_for_a_forbidden_word(word: str) -> None:
    evil = Migration(4, "evil", f"SELECT 1; {word.upper()} x;")
    with pytest.raises(MigrationError, match="touches the audit guard"):
        guarded((*MIGRATIONS, evil))


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE public.assessments ENABLE REPLICA TRIGGER ALL",
        "ALTER TABLE public.assessments ENABLE TRIGGER ALL",
        "ALTER TABLE public.assessments ENABLE ALWAYS TRIGGER USER",
        "ALTER TABLE x ENABLE TRIGGER y",
        "ALTER TABLE x DISABLE TRIGGER ALL",
        "ALTER TABLE x DISABLE   TRIGGER y",
        "ALTER TABLE x DISABLE\n    TRIGGER y",
        "ALTER TABLE x DISABLE\tTRIGGER y",
        "ALTER TABLE x ENABLE\nREPLICA TRIGGER y",
    ],
)
def test_trigger_state_changes_are_refused_in_any_spacing(sql: str) -> None:
    evil = Migration(4, "evil", sql)
    with pytest.raises(MigrationError, match="touches the audit guard"):
        guarded((*MIGRATIONS, evil))


def test_the_reviewed_exception_table_cannot_be_changed_at_run_time() -> None:
    from divesafe.services.migrations import REVIEWED_GUARD_MIGRATIONS

    with pytest.raises(TypeError):
        REVIEWED_GUARD_MIGRATIONS[4] = "0" * 64  # type: ignore[index]
