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
    # Owner decision 2026-11-14, option A: migration 6 (ADR 0028) is an unreviewed later migration,
    # so the word scan covers it; it is pinned to its exact SQL below instead of a review checksum.
    assert [m.version for m in unreviewed] == [2, 6]
    guarded((MIGRATIONS[0], *unreviewed))
    assert _normalised(MIGRATIONS[5].sql) == MIGRATION_SIX_SQL


# ADR 0028 decision 7: migration 6 is exactly this one statement. Any edit to it fails here.
MIGRATION_SIX_SQL = (
    "ALTER TABLE public.kb_documents ADD COLUMN attests_model_host_egress boolean NOT NULL "
    "DEFAULT false"
)


def _normalised(sql: str) -> str:
    """Whitespace collapsed and one trailing semicolon dropped; nothing else is forgiven."""
    text = " ".join(sql.split())
    return text.removesuffix(";").rstrip()


def test_migration_six_is_version_six_and_the_last_shipped_migration() -> None:
    assert [m.version for m in MIGRATIONS] == [1, 2, 3, 4, 5, 6]
    assert MIGRATIONS[5].version == 6
    assert ";" not in _normalised(MIGRATIONS[5].sql)  # one statement only


@pytest.mark.parametrize(
    "edited",
    [
        MIGRATION_SIX_SQL.replace("DEFAULT false", "DEFAULT true"),
        MIGRATION_SIX_SQL.replace(" NOT NULL", ""),
        MIGRATION_SIX_SQL + "; GRANT UPDATE ON public.kb_documents TO PUBLIC",
        MIGRATION_SIX_SQL.lower(),
    ],
    ids=["default-true", "nullable", "second-statement", "case-changed"],
)
def test_mutation_an_edited_migration_six_fails_the_exact_sql_pin(edited: str) -> None:
    """The pin's own teeth: whitespace is the only thing normalised away."""
    assert _normalised(edited) != MIGRATION_SIX_SQL
    assert _normalised("\n  " + MIGRATION_SIX_SQL.replace(" ", "\n    ") + ";\n") == (
        MIGRATION_SIX_SQL
    )


@pytest.mark.parametrize("word", FORBIDDEN_IN_LATER_MIGRATIONS)
def test_migration_six_gets_no_exception_from_the_word_scan(word: str) -> None:
    six = MIGRATIONS[5]
    evil = Migration(6, six.name, f"{six.sql}\nSELECT 1; {word.upper()} x;")
    with pytest.raises(MigrationError, match="touches the audit guard"):
        guarded((*MIGRATIONS[:5], evil))


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


REVIEWED_FOUR = "c95979fbf982c54ef0f05528d9549944d49a10c0210690854509c9042a36df8e"

REVIEWED_FIVE = "76afa4028d3cd312dfd1b8a472b584b639077cd2a35a2e3b529d75c1ae685f40"


def test_migrations_three_four_and_five_are_pinned_and_are_the_only_reviewed_exceptions() -> None:
    from divesafe.services.migrations import REVIEWED_GUARD_MIGRATIONS

    assert MIGRATIONS[2].version == 3
    assert MIGRATIONS[2].checksum == REVIEWED_THREE
    assert MIGRATIONS[3].version == 4
    assert MIGRATIONS[3].checksum == REVIEWED_FOUR
    assert MIGRATIONS[4].version == 5
    assert MIGRATIONS[4].checksum == REVIEWED_FIVE
    assert dict(REVIEWED_GUARD_MIGRATIONS) == {
        3: REVIEWED_THREE,
        4: REVIEWED_FOUR,
        5: REVIEWED_FIVE,
    }
    assert set(REVIEWED_GUARD_MIGRATIONS) == {3, 4, 5}


def test_the_reviewed_table_is_read_only_for_every_operation() -> None:
    from divesafe.services.migrations import REVIEWED_GUARD_MIGRATIONS

    with pytest.raises(TypeError):
        REVIEWED_GUARD_MIGRATIONS[5] = "0" * 64  # type: ignore[index]
    with pytest.raises(TypeError):
        REVIEWED_GUARD_MIGRATIONS[4] = "0" * 64  # type: ignore[index]
    with pytest.raises(TypeError):
        del REVIEWED_GUARD_MIGRATIONS[3]  # type: ignore[attr-defined]
    assert not hasattr(REVIEWED_GUARD_MIGRATIONS, "update")
    assert not hasattr(REVIEWED_GUARD_MIGRATIONS, "pop")


def test_an_edited_copy_of_migration_four_is_refused_by_the_word_scan() -> None:
    edited = Migration(4, MIGRATIONS[3].name, MIGRATIONS[3].sql + "\n-- edited")
    assert edited.checksum != REVIEWED_FOUR
    with pytest.raises(MigrationError, match="touches the audit guard"):
        guarded((*MIGRATIONS[:3], edited))


def test_migration_four_sql_under_another_version_number_is_refused() -> None:
    four = MIGRATIONS[3]
    as_five = Migration(5, four.name, four.sql)
    with pytest.raises(MigrationError):
        guarded((*MIGRATIONS, as_five))
    as_three = Migration(3, four.name, four.sql)
    with pytest.raises(MigrationError, match="touches the audit guard"):
        guarded((MIGRATIONS[0], MIGRATIONS[1], as_three))
    as_two = Migration(2, four.name, four.sql)
    with pytest.raises(MigrationError, match="touches the audit guard"):
        guarded((MIGRATIONS[0], as_two))


def test_migration_three_sql_at_version_five_is_refused() -> None:
    three = MIGRATIONS[2]
    with pytest.raises(MigrationError, match="touches the audit guard"):
        guarded((*MIGRATIONS, Migration(5, three.name, three.sql)))


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
