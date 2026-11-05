"""ADR 0026 Part B step 3: `groups` scope what a viewer key may read."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.safety.kb_fakes import mutated_module
from tests.safety.test_api_human_gate import KEY_A, KEY_B, _build, _create, _hash

from divesafe.api import app as app_module
from divesafe.api import auth as auth_module
from divesafe.api.auth import ApiKeyAuthenticator, KeyEntry, Role, parse_api_key_hashes

pytestmark = pytest.mark.safety

KEY_ERIN, KEY_FRED, KEY_OPEN = "key-erin-test-only", "key-fred-test-only", "key-open-test-only"
VIEW = frozenset({Role.VIEWER})
DECIDER = frozenset({Role.VIEWER, Role.DECIDER})
AUTH = {n: {"Authorization": f"Bearer {k}"} for n, k in (("bob", KEY_B), ("erin", KEY_ERIN))}
AUTH["fred"] = {"Authorization": f"Bearer {KEY_FRED}"}
AUTH["open"] = {"Authorization": f"Bearer {KEY_OPEN}"}


def _keys() -> dict[str, KeyEntry]:
    return {
        # alice creates (no groups: she is the creator and reads everything she is given)
        _hash(KEY_A): KeyEntry("alice", frozenset(Role) - {Role.ADMIN}, frozenset({"north"})),
        _hash(KEY_B): KeyEntry("bob", DECIDER, frozenset({"north"})),
        _hash(KEY_ERIN): KeyEntry("erin", VIEW, frozenset({"north"})),
        _hash(KEY_FRED): KeyEntry("fred", VIEW, frozenset({"south"})),
        _hash(KEY_OPEN): KeyEntry("opal", VIEW),
    }


def _setup(module: Any = app_module) -> tuple[TestClient, TestClient, str]:
    client, _, _ = _build()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.authenticator = ApiKeyAuthenticator(_keys())
    rid = _create(client, decider="bob")["record"]["id"]
    return client, TestClient(module.create_app(state)), rid


def _status(client: TestClient, who: str, rid: str, suffix: str = "") -> int:
    return client.get(f"/v1/assessments/{rid}{suffix}", headers=AUTH[who]).status_code


def test_a_viewer_in_the_same_group_can_read_and_another_group_gets_404() -> None:
    client, _, rid = _setup()
    for suffix in ("", "/evidence"):
        assert _status(client, "erin", rid, suffix) == 200
        assert _status(client, "fred", rid, suffix) == 404
    assert _status(client, "bob", rid) == 200  # the assigned decider


def test_an_unreadable_assessment_looks_exactly_like_a_missing_one() -> None:
    client, _, rid = _setup()
    for suffix in ("", "/evidence"):
        hidden = client.get(f"/v1/assessments/{rid}{suffix}", headers=AUTH["fred"])
        missing = client.get(f"/v1/assessments/{'f' * 32}{suffix}", headers=AUTH["fred"])
        assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json())


def test_a_key_without_groups_still_reads_everything() -> None:
    client, _, rid = _setup()
    assert _status(client, "open", rid) == 200
    assert _status(client, "open", rid, "/evidence") == 200


def test_an_out_of_scope_viewer_cannot_decide_or_move_or_learn_anything() -> None:
    client, _, rid = _setup()
    out = client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "NO-GO", "rationale": "x"},
        headers=AUTH["fred"],
    )
    assert out.status_code in (403, 404)
    assert rid not in json.dumps(out.json())


def _with_a_running_run() -> tuple[TestClient, str]:
    from datetime import timedelta

    from divesafe.services import RunState
    from divesafe.services.runs import PendingRun

    client, _, repo = _build()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.authenticator = ApiKeyAuthenticator(_keys())
    now = state.clock()
    run = PendingRun(
        id="c" * 32,
        created_by="alice",
        assigned_decider="bob",
        plan=_create(client, decider="bob")["record"]["plan"],
        created_at=now,
        state=RunState.RUNNING,
        lease_expires_at=now + timedelta(minutes=5),
    )
    repo._runs[run.id] = run  # type: ignore[attr-defined]
    return client, run.id


def test_a_run_in_progress_is_hidden_from_another_group_too() -> None:
    client, rid = _with_a_running_run()
    assert _status(client, "erin", rid) == 200  # same group: the status
    for suffix in ("", "/evidence"):
        hidden = client.get(f"/v1/assessments/{rid}{suffix}", headers=AUTH["fred"])
        missing = client.get(f"/v1/assessments/{'f' * 32}{suffix}", headers=AUTH["fred"])
        assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json())


def test_groups_are_parsed_bounded_and_case_insensitive() -> None:
    def parse(groups: Any) -> Any:
        cfg = {"a" * 64: {"actor": "erin", "roles": ["viewer"], "groups": groups}}
        return parse_api_key_hashes(json.dumps(cfg))["a" * 64].groups

    assert parse(["North", " north ", "South"]) == frozenset({"north", "south"})
    for bad in ("north", [1], [""], ["x"] * 21, [["north"]], None):
        with pytest.raises(ValueError):
            parse(bad)
    with pytest.raises(ValueError):  # an unknown entry key is still refused
        parse_api_key_hashes(
            json.dumps({"a" * 64: {"actor": "e", "roles": ["viewer"], "team": ["x"]}})
        )


def test_production_warns_while_a_viewer_key_has_no_groups(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from divesafe.config import Settings

    def settings(raw: dict[str, Any], env: str) -> Settings:
        return Settings(
            environment=env,
            api_key_hashes=json.dumps(raw),
            llm_provider="openai_compatible",
        )  # type: ignore[arg-type]

    open_key = {"a" * 64: {"actor": "opal", "roles": ["viewer"]}}
    scoped = {"a" * 64: {"actor": "erin", "roles": ["viewer"], "groups": ["north"]}}
    with caplog.at_level(logging.WARNING, logger=auth_module.logger.name):
        auth_module.build_authenticator(settings(open_key, "production"))
        assert any("no groups" in r.getMessage() for r in caplog.records)
        caplog.clear()
        auth_module.build_authenticator(settings(scoped, "production"))
        assert not any("no groups" in r.getMessage() for r in caplog.records)


# --- mutations: the weakened copy must let the leak through ---------------------------------------


def test_mutation_a_viewer_of_another_group_can_read() -> None:
    mutant = mutated_module(
        app_module,
        ("        if not principal.groups:\n            return True", "        return True"),
    )
    _, target, rid = _setup(mutant)
    assert target.get(f"/v1/assessments/{rid}", headers=AUTH["fred"]).status_code == 200


def test_mutation_the_evidence_path_is_not_scoped() -> None:
    mutant = mutated_module(
        app_module,
        (
            "        return _scoped(record, principal)\n\n    def _scoped",
            "        return record\n\n    def _scoped",
        ),
    )
    _, target, rid = _setup(mutant)
    assert target.get(f"/v1/assessments/{rid}/evidence", headers=AUTH["fred"]).status_code == 200


def test_mutation_groups_are_ignored_when_reading_one_assessment() -> None:
    mutant = mutated_module(
        app_module,
        (
            "                return AssessmentView.of(_scoped(record, principal))",
            "                return AssessmentView.of(record)",
        ),
    )
    _, target, rid = _setup(mutant)
    assert target.get(f"/v1/assessments/{rid}", headers=AUTH["fred"]).status_code == 200


def test_mutation_a_group_is_matched_case_sensitively_or_not_at_all() -> None:
    mutant = mutated_module(
        auth_module, ("        names.add(name.casefold())", "        names.add(name)")
    )
    cfg = {"a" * 64: {"actor": "e", "roles": ["viewer"], "groups": ["North"]}}
    assert mutant.parse_api_key_hashes(json.dumps(cfg))["a" * 64].groups == {"North"}
    assert auth_module.parse_api_key_hashes(json.dumps(cfg))["a" * 64].groups == {"north"}


def test_mutation_the_production_warning_is_removed() -> None:
    mutant = mutated_module(
        auth_module, ('settings.environment == "production" and any(', "False and any(")
    )
    from divesafe.config import Settings

    s = Settings(
        environment="production",
        api_key_hashes=json.dumps({"a" * 64: {"actor": "o", "roles": ["viewer"]}}),
        llm_provider="openai_compatible",
    )  # type: ignore[arg-type]
    records: list[str] = []

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = _H()
    mutant.logger.addHandler(handler)
    try:
        mutant.build_authenticator(s)
    finally:
        mutant.logger.removeHandler(handler)
    assert not any("no groups" in m for m in records)
