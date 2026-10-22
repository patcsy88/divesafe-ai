"""The knowledge loader role name and TEMPORARY checks (ADR 0022 decision 11). Database-free.

The role may never be PUBLIC, a built-in `pg_*` role, the application role or an owner/bootstrap
role, and no refusal echoes the value. A TEMPORARY privilege that survives the revokes is
refused."""

from __future__ import annotations

import inspect
import textwrap
from typing import Any

import pytest
from pydantic import ValidationError

from divesafe.config import Settings
from divesafe.config import roles as config_roles
from divesafe.services import db_init
from divesafe.services import postgres as postgres_module
from divesafe.services.postgres import (
    LOADER_ROLE_INVALID,
    TEMP_STILL_HELD,
    LoaderRoleError,
    TempPrivilegeError,
    check_loader_role_name,
)

pytestmark = pytest.mark.safety

# Derived from the single source of truth, so the list cannot drift from the code under test.
BAD = tuple(dict.fromkeys(("public", "pg_x", "pg_monitor", *config_roles.RESERVED_ROLE_NAMES)))


def test_the_reserved_names_are_the_ones_the_policy_names_and_postgres_uses_the_same_object() -> (
    None
):
    # Pinned so that emptying or shrinking the shared tuple fails here, not silently everywhere.
    assert {"divesafe_app", "divesafe_owner", "divesafe_bootstrap"} <= set(
        config_roles.RESERVED_ROLE_NAMES
    )
    assert config_roles.DEFAULT_APP_ROLE in config_roles.RESERVED_ROLE_NAMES
    assert postgres_module.RESERVED_ROLE_NAMES is config_roles.RESERVED_ROLE_NAMES
    import divesafe.config.settings as settings_module

    assert settings_module.RESERVED_ROLE_NAMES is config_roles.RESERVED_ROLE_NAMES


@pytest.mark.parametrize("role", config_roles.RESERVED_ROLE_NAMES)
def test_every_reserved_name_is_refused_by_settings_and_by_postgres(role: str) -> None:
    with pytest.raises(ValidationError):
        Settings(loader_db_role=role)
    with pytest.raises(ValueError):
        check_loader_role_name(role)
    with pytest.raises(ValueError):
        check_loader_role_name(role, "some_other_app")


def test_settings_refuse_a_loader_equal_to_the_application_role_without_echoing_it() -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(app_db_role="custom_app_role", loader_db_role="custom_app_role")
    assert "custom_app_role" not in str(caught.value)
    Settings(app_db_role="custom_app_role", loader_db_role="custom_loader_role")
    Settings(app_db_role=None, loader_db_role="custom_loader_role")


def test_db_init_turns_a_bad_application_role_into_a_fixed_exit_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIVESAFE_ADMIN_DATABASE_URL", "postgresql://u:never-echoed@127.0.0.1:1/x")
    monkeypatch.setenv("DIVESAFE_LOADER_DB_ROLE", "divesafe_some_loader")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise ValueError("bad-app-role-secret never-echoed")

    monkeypatch.setattr(db_init, "install_schema", fail)
    with pytest.raises(SystemExit) as caught:
        db_init.main()
    assert caught.value.code == db_init.APP_ROLE_INVALID == "invalid application role name"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert "bad-app-role-secret" not in str(caught.value.code)
    assert "never-echoed" not in str(caught.value.code)


@pytest.mark.parametrize("role", BAD)
def test_check_loader_role_name_refuses_reserved_names_with_a_fixed_message(role: str) -> None:
    with pytest.raises(ValueError) as caught:
        check_loader_role_name(role)
    assert LOADER_ROLE_INVALID in str(caught.value) or "invalid" in str(caught.value)
    assert role not in str(caught.value)


def test_the_application_role_is_refused_even_when_it_has_another_name() -> None:
    with pytest.raises(ValueError, match="invalid loader role name"):
        check_loader_role_name("custom_app", "custom_app")
    check_loader_role_name("custom_loader", "custom_app")


def test_divesafe_app_is_refused_when_the_application_role_setting_is_unset() -> None:
    with pytest.raises(ValueError, match="invalid loader role name"):
        check_loader_role_name("divesafe_app", None)


def test_an_ordinary_name_is_accepted() -> None:
    check_loader_role_name("divesafe_loader", "divesafe_app")


@pytest.mark.parametrize("role", BAD)
def test_the_settings_validator_refuses_the_same_names_without_echoing_them(role: str) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(loader_db_role=role)
    text = str(caught.value)
    assert "DIVESAFE_LOADER_DB_ROLE is not an acceptable role name" in text
    assert f"'{role}'" not in text and f"={role}" not in text


@pytest.mark.parametrize("role", BAD)
def test_db_init_exits_with_the_fixed_text_for_a_refused_name(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings normally refuse these first; with a settings object that lets one through,
    `db_init` itself exits with the fixed text, before any connection, and never echoes it."""
    settings = Settings.model_construct(
        admin_database_url=Settings(
            admin_database_url="postgresql://u:never-echoed@127.0.0.1:1/x_test"  # type: ignore[arg-type]
        ).admin_database_url,
        app_db_role=None,
        loader_db_role=role,
    )
    monkeypatch.setattr(db_init, "load_settings", lambda: settings)

    def never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("install_schema must not be reached")

    monkeypatch.setattr(db_init, "install_schema", never)
    with pytest.raises(SystemExit) as caught:
        db_init.main()
    assert caught.value.code == LOADER_ROLE_INVALID
    assert role not in str(caught.value.code)
    assert "never-echoed" not in str(caught.value.code)


@pytest.mark.parametrize("role", BAD)
def test_db_init_through_the_environment_never_prints_the_value_or_the_password(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIVESAFE_ADMIN_DATABASE_URL", "postgresql://u:never-echoed@127.0.0.1:1/x")
    monkeypatch.setenv("DIVESAFE_LOADER_DB_ROLE", role)
    with pytest.raises((SystemExit, ValidationError)) as caught:
        db_init.main()
    assert "never-echoed" not in str(caught.value)
    assert f"'{role}'" not in str(caught.value)


def test_db_init_turns_a_loader_role_error_into_its_fixed_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIVESAFE_ADMIN_DATABASE_URL", "postgresql://u:never-echoed@127.0.0.1:1/x")
    monkeypatch.setenv("DIVESAFE_LOADER_DB_ROLE", "divesafe_some_loader")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise TempPrivilegeError(TEMP_STILL_HELD)

    monkeypatch.setattr(db_init, "install_schema", fail)
    with pytest.raises(SystemExit) as caught:
        db_init.main()
    assert caught.value.code == TEMP_STILL_HELD


# --- TEMPORARY that stays held after the revokes ----------------------------------------------


class _Result:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _FakeConn:
    """Answers only the reads `_kb_grants` makes; every REVOKE/GRANT is accepted and ignored."""

    def __init__(
        self,
        public_temp: Any,
        loader_temp: Any,
        holders: tuple[str, ...] = (),
        members: tuple[str, ...] = (),
    ) -> None:
        self.public_temp = public_temp
        self.loader_temp = loader_temp
        self.holders = holders  # other roles that hold TEMPORARY on the database
        self.members = members  # the roles the loader is a member of

    def execute(self, query: Any, params: Any = None) -> _Result:
        if isinstance(query, str):
            if query == "SELECT current_database()":
                return _Result(("scratch_test",))
            if "has_database_privilege('public'" in query:
                return _Result((self.public_temp,))
            if "has_database_privilege(%s" in query:
                return _Result((self.loader_temp,))
            if "FROM pg_roles r" in query:
                # Without the membership clause every TEMPORARY holder is returned.
                member_only = "pg_has_role(%s" in query
                rows = [h for h in self.holders if not member_only or h in self.members]
                return _Result((rows[0],) if rows else None)
        return _Result(None)


def run_grants(
    public_temp: Any,
    loader_temp: Any,
    loader: str | None = "a_loader",
    holders: tuple[str, ...] = (),
    members: tuple[str, ...] = (),
) -> None:
    conn = _FakeConn(public_temp, loader_temp, holders, members)
    postgres_module._kb_grants(conn, "an_app", loader)  # type: ignore[arg-type]


def test_temporary_released_everywhere_is_accepted() -> None:
    run_grants(False, False)
    run_grants(False, None, loader=None)


@pytest.mark.parametrize(
    ("public_temp", "loader_temp"),
    [(True, False), (False, True), (True, True), (None, False), (False, None)],
)
def test_temporary_still_held_is_refused_with_the_fixed_message(
    public_temp: Any, loader_temp: Any
) -> None:
    with pytest.raises(TempPrivilegeError) as caught:
        run_grants(public_temp, loader_temp)
    assert str(caught.value) == TEMP_STILL_HELD
    assert isinstance(caught.value, LoaderRoleError)
    assert "a_loader" not in str(caught.value)


def test_public_temporary_alone_is_refused_when_there_is_no_loader_role() -> None:
    with pytest.raises(TempPrivilegeError):
        run_grants(True, None, loader=None)


def _mutated_grants(old: str, new: str) -> Any:
    """A COPY of `_kb_grants` compiled from its source with one edit; `src` is not touched."""
    source = textwrap.dedent(inspect.getsource(postgres_module._kb_grants))
    assert old in source, old
    namespace: dict[str, Any] = {}
    exec(source.replace(old, new, 1), vars(postgres_module), namespace)  # noqa: S102
    return namespace["_kb_grants"]


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        ("raise removed", "raise TempPrivilegeError(TEMP_STILL_HELD)", "pass"),
        ("held test inverted", "if held:", "if not held:"),
        ("loader read-back dropped", "held = held or loader_temp", "held = held and loader_temp"),
        ("public read-back dropped", "held = public_temp is None or", "held = False and"),
    ],
)
def test_mutation_the_temporary_read_back_is_what_refuses(label: str, old: str, new: str) -> None:
    mutated = _mutated_grants(old, new)
    cases = [(True, False), (False, True), (True, True)]
    caught = 0
    for public_temp, loader_temp in cases:
        try:
            mutated(_FakeConn(public_temp, loader_temp), "an_app", "a_loader")
        except TempPrivilegeError:
            continue
        caught += 1  # the mutated copy let a held TEMPORARY through
    assert caught >= 1, label
    # and the real function refuses every one of those cases
    for public_temp, loader_temp in cases:
        with pytest.raises(TempPrivilegeError):
            run_grants(public_temp, loader_temp)


# --- TEMPORARY inherited through a role the loader is a member of (NOINHERIT / SET ROLE) ----------


def test_temporary_held_by_a_role_the_loader_is_a_member_of_is_refused() -> None:
    with pytest.raises(TempPrivilegeError) as caught:
        run_grants(False, False, holders=("a_group",), members=("a_group",))
    assert str(caught.value) == TEMP_STILL_HELD
    assert "a_group" not in str(caught.value)


def test_temporary_held_by_an_unrelated_role_is_not_the_loaders_concern() -> None:
    run_grants(False, False, holders=("some_other_role",), members=("a_group",))


def test_the_membership_read_back_is_not_made_without_a_loader_role() -> None:
    run_grants(False, None, loader=None, holders=("a_group",), members=("a_group",))


def test_mutation_without_the_membership_read_back_a_held_temporary_gets_through() -> None:
    mutated = _mutated_grants("held = held or member_temp is not None", "pass")
    mutated(_FakeConn(False, False, ("a_group",), ("a_group",)), "an_app", "a_loader")
    with pytest.raises(TempPrivilegeError):  # the test above is what notices
        run_grants(False, False, holders=("a_group",), members=("a_group",))


def test_mutation_without_the_member_clause_an_unrelated_holder_is_refused() -> None:
    mutated = _mutated_grants("AND COALESCE(pg_has_role(%s, r.oid, 'MEMBER'), true) ", "")
    with pytest.raises(TempPrivilegeError):
        mutated(_FakeConn(False, False, ("unrelated",), ()), "an_app", "a_loader")
    run_grants(False, False, holders=("unrelated",), members=())  # real code accepts it


# --- db_init without a loader role: a bad application role --------------------------------------


def _main_without_loader(monkeypatch: pytest.MonkeyPatch, app_role: str) -> Any:
    settings = Settings.model_construct(
        admin_database_url=Settings(
            admin_database_url="postgresql://u:never-echoed@127.0.0.1:1/x_test"  # type: ignore[arg-type]
        ).admin_database_url,
        app_db_role=app_role,
        loader_db_role=None,
    )
    monkeypatch.setattr(db_init, "load_settings", lambda: settings)
    return db_init.main


@pytest.mark.parametrize("app_role", ["public", "pg_monitor"])
def test_db_init_without_a_loader_role_refuses_a_bad_application_role_with_a_fixed_exit(
    monkeypatch: pytest.MonkeyPatch, app_role: str
) -> None:
    """The real `install_schema` refuses the name before any connection (port 1 is closed)."""
    main = _main_without_loader(monkeypatch, app_role)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == db_init.APP_ROLE_INVALID == "invalid application role name"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True
    assert app_role not in str(caught.value.code)
    assert "never-echoed" not in str(caught.value.code)


def test_db_init_without_a_loader_role_hides_what_the_value_error_said(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _main_without_loader(monkeypatch, "public")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise ValueError("secret-detail never-echoed")

    monkeypatch.setattr(db_init, "install_schema", fail)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == db_init.APP_ROLE_INVALID
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True
    assert "secret-detail" not in str(caught.value.code)


def _mutated_main(old: str, new: str) -> Any:
    """A COPY of `db_init.main` compiled from its source with one edit; `src` is not touched."""
    source = textwrap.dedent(inspect.getsource(db_init.main))
    assert source.count(old) == 1, old
    namespace: dict[str, Any] = {}
    exec(source.replace(old, new), vars(db_init), namespace)  # noqa: S102
    return namespace["main"]


NO_LOADER_HANDLER = (
    "        except ValueError:\n            raise SystemExit(APP_ROLE_INVALID) from None\n"
)


def test_mutation_without_the_handler_the_value_error_escapes_the_no_loader_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutated = _mutated_main(NO_LOADER_HANDLER, "")
    _main_without_loader(monkeypatch, "public")
    with pytest.raises(ValueError):  # a traceback for the operator: the tests above notice
        mutated()
    with pytest.raises(SystemExit):
        db_init.main()


def test_mutation_a_handler_that_keeps_the_cause_leaves_a_traceback_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutated = _mutated_main(
        NO_LOADER_HANDLER,
        "        except ValueError:\n            raise SystemExit(APP_ROLE_INVALID)\n",
    )
    _main_without_loader(monkeypatch, "public")
    with pytest.raises(SystemExit) as caught:
        mutated()
    assert caught.value.__suppress_context__ is False  # the check above asserts True
    assert caught.value.__context__ is not None


# --- nested membership: loader -> middle -> group holding TEMPORARY -------------------------------


def test_temporary_held_through_a_nested_membership_is_refused() -> None:
    """`pg_has_role(..., 'MEMBER')` is transitive; the fake mirrors that with the member list."""
    with pytest.raises(TempPrivilegeError) as caught:
        run_grants(False, False, holders=("top_group",), members=("middle", "top_group"))
    assert str(caught.value) == TEMP_STILL_HELD
    assert "top_group" not in str(caught.value) and "middle" not in str(caught.value)
    run_grants(False, False, holders=("other_group",), members=("middle", "top_group"))


def test_the_query_asks_for_membership_not_direct_membership() -> None:
    seen: list[str] = []

    class Spy(_FakeConn):
        def execute(self, query: Any, params: Any = None) -> _Result:
            seen.append(str(query))
            return super().execute(query, params)

    postgres_module._kb_grants(Spy(False, False), "an_app", "a_loader")  # type: ignore[arg-type]
    member_query = next(q for q in seen if "FROM pg_roles r" in q)
    assert "'MEMBER'" in member_query  # transitive; 'USAGE' would be inheritance-only
    assert "pg_auth_members" not in member_query  # a direct-membership join would miss a chain
