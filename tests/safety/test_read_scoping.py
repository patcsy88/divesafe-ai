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
KEY_DORA, KEY_CARA, KEY_GUS, KEY_ROOT = (
    "key-dora-test-only",
    "key-cara-test-only",
    "key-gus-test-only",
    "key-root-test-only",
)
VIEW = frozenset({Role.VIEWER})
DECIDER = frozenset({Role.VIEWER, Role.DECIDER, Role.OVERRIDER})
AUTH = {
    n: {"Authorization": f"Bearer {k}"}
    for n, k in (
        ("alice", KEY_A),
        ("bob", KEY_B),
        ("erin", KEY_ERIN),
        ("fred", KEY_FRED),
        ("open", KEY_OPEN),
        ("dora", KEY_DORA),
        ("cara", KEY_CARA),
        ("gus", KEY_GUS),
        ("root", KEY_ROOT),
    )
}


def _keys() -> dict[str, KeyEntry]:
    """alice (creator) and bob are in `north`; dora (decider) and gus are in `east`, which shares
    nothing with alice; cara (decider) and fred are in `south`; root (admin) is in `west`."""
    north, east, south = frozenset({"north"}), frozenset({"east"}), frozenset({"south"})
    return {
        _hash(KEY_A): KeyEntry("alice", frozenset(Role) - {Role.ADMIN}, north),
        _hash(KEY_B): KeyEntry("bob", DECIDER, north),
        _hash(KEY_ERIN): KeyEntry("erin", VIEW, north),
        _hash(KEY_FRED): KeyEntry("fred", VIEW, south),
        _hash(KEY_OPEN): KeyEntry("opal", VIEW),
        _hash(KEY_DORA): KeyEntry("dora", DECIDER, east),
        _hash(KEY_CARA): KeyEntry("cara", DECIDER, south),
        _hash(KEY_GUS): KeyEntry("gus", VIEW, east),
        _hash(KEY_ROOT): KeyEntry(
            "root", frozenset({Role.VIEWER, Role.ADMIN}), frozenset({"west"})
        ),
    }


class _NoLookup(ApiKeyAuthenticator):
    """Group lookup by name finds nothing, so only the actor-name match can grant a read."""

    def __init__(self) -> None:
        super().__init__(_keys())

    def groups_of(self, actor: str) -> frozenset[str]:
        return frozenset()


def _setup(
    module: Any = app_module, decider: str = "bob", auth: Any = None
) -> tuple[TestClient, TestClient, str]:
    client, _, _ = _build()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.authenticator = auth or ApiKeyAuthenticator(_keys())
    rid = _create(client, decider=decider)["record"]["id"]
    return client, TestClient(module.create_app(state)), rid


def _status(client: TestClient, who: str, rid: str, suffix: str = "") -> int:
    return client.get(f"/v1/assessments/{rid}{suffix}", headers=AUTH[who]).status_code


def _decide(client: TestClient, who: str, rid: str) -> Any:
    body = {"decision": "NO-GO", "rationale": "Too risky for this group."}
    return client.post(f"/v1/assessments/{rid}/decision", json=body, headers=AUTH[who])


def _move(client: TestClient, rid: str, to: str, who: str = "root") -> Any:
    return client.post(
        f"/v1/assessments/{rid}/reassign", json={"to_decider": to}, headers=AUTH[who]
    )


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


def test_a_decider_in_another_group_reads_and_decides_and_a_colleague_reads() -> None:
    client, _, rid = _setup(decider="dora")
    for suffix in ("", "/evidence"):
        assert _status(client, "dora", rid, suffix) == 200
        assert _status(client, "gus", rid, suffix) == 200  # shares a group with the decider only
        assert _status(client, "erin", rid, suffix) == 200  # shares a group with the creator
        assert _status(client, "fred", rid, suffix) == 404
    actual = client.post(
        f"/v1/assessments/{rid}/actual-conditions",
        json={"observations": {}},
        headers=AUTH["dora"],
    )
    assert actual.status_code != 404
    assert _decide(client, "dora", rid).status_code == 200


def test_an_out_of_scope_decider_gets_404_and_nothing_is_decided() -> None:
    client, _, rid = _setup()
    out = _decide(client, "cara", rid)
    missing = _decide(client, "cara", "f" * 32)
    assert (
        (out.status_code, out.json()) == (missing.status_code, missing.json()) == (404, out.json())
    )
    assert (
        client.get(f"/v1/assessments/{rid}", headers=AUTH["bob"]).json()["record"]["human_decision"]
        is None
    )


def test_the_previous_decider_still_sees_the_assessment_and_cannot_decide_it() -> None:
    client, _, rid = _setup(decider="dora")
    assert _move(client, rid, "cara").status_code == 200
    for suffix in ("", "/evidence"):
        assert _status(client, "dora", rid, suffix) == 200  # moved off, still named in the record
        assert _status(client, "cara", rid, suffix) == 200  # the new decider, in another group
    assert _decide(client, "dora", rid).status_code == 403
    assert _decide(client, "cara", rid).status_code == 200


def test_an_admin_reads_and_moves_every_pending_assessment_but_not_a_decided_one() -> None:
    client, _, rid = _setup()  # root is in `west`, which shares nothing with anyone involved
    assert _status(client, "root", rid) == 200
    assert _status(client, "root", rid, "/evidence") == 200
    assert _move(client, rid, "dora").status_code == 200  # and can move it out of every view
    assert _status(client, "root", rid) == 200
    assert _decide(client, "dora", rid).status_code == 200
    gone = _move(client, rid, "cara")
    assert gone.status_code == 404  # decided: scoped like everything else
    assert _status(client, "root", rid) == 404
    assert _status(client, "root", rid, "/evidence") == 404


def test_whoever_may_decide_may_also_read() -> None:
    from divesafe.orchestration.human_gate import NotAssignedDeciderError, check_assigned

    client, _, rid = _setup(decider="dora")
    _move(client, rid, "cara")
    record = client.app.state.divesafe.repository.get(rid)  # type: ignore[attr-defined]
    deciders = [k.actor for k in _keys().values() if Role.DECIDER in k.roles]
    allowed = []
    for actor in deciders:
        try:
            check_assigned(record, actor)
        except NotAssignedDeciderError:
            continue
        allowed.append(actor)
    assert allowed == ["cara"]
    for actor in allowed:
        assert _status(client, actor, rid) == 200


def _with_a_running_run(module: Any = app_module) -> tuple[TestClient, str]:
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
    return TestClient(module.create_app(state)), run.id


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


def _app_mutant(old: str, new: str) -> Any:
    return mutated_module(app_module, (old, new))


NAMES = "names = [record.created_by, record.assigned_decider, *moves]"
LOAD = "record = _load(assessment_id, principal)\n"
ASSIGNED = "        _require_assigned(record, principal)\n"
RUN_SCOPE = "and _in_scope((run.created_by, run.assigned_decider), principal, pending=True)"
NAME_CHECK = "if principal.actor.strip().casefold() in {n.strip().casefold() for n in people}:"


def _unscoped(text: str) -> str:
    return text.replace("_load(assessment_id, principal)", "_load(assessment_id)")


def test_mutation_a_viewer_of_another_group_can_read() -> None:
    mutant = _app_mutant("if not principal.groups or (", "if True or (")
    _, target, rid = _setup(mutant)
    assert _status(target, "fred", rid) == 200


def test_mutation_the_name_check_is_removed() -> None:
    _, real, rid = _setup(decider="dora", auth=_NoLookup())
    for suffix in ("", "/evidence"):  # no group lookup at all: the name alone must be enough
        assert _status(real, "dora", rid, suffix) == 200
    mutant = _app_mutant(NAME_CHECK, "if False:")
    _, target, rid = _setup(mutant, decider="dora", auth=_NoLookup())
    assert _status(target, "dora", rid) == 404
    assert _status(target, "dora", rid, "/evidence") == 404


def test_mutation_the_assignee_is_not_in_the_names() -> None:
    mutant = _app_mutant(NAMES, "names = [record.created_by, *moves]")
    _, target, rid = _setup(mutant, decider="dora")
    assert _status(target, "dora", rid) == 404
    assert _status(target, "gus", rid) == 404  # a colleague of the decider


def test_mutation_the_creator_is_not_in_the_names() -> None:
    mutant = _app_mutant(NAMES, "names = [record.assigned_decider, *moves]")
    _, target, rid = _setup(mutant, decider="dora")
    assert _status(target, "erin", rid) == 404  # a colleague of the creator


def test_mutation_the_previous_decider_is_hidden() -> None:
    mutant = _app_mutant(NAMES, "names = [record.created_by, record.assigned_decider]")
    client, target, rid = _setup(mutant, decider="dora")
    assert _move(client, rid, "cara").status_code == 200
    assert _status(target, "dora", rid) == 404


def test_mutation_the_admin_is_scoped_on_a_pending_assessment() -> None:
    mutant = _app_mutant("(pending and Role.ADMIN", "(False and Role.ADMIN")
    _, target, rid = _setup(mutant)
    assert _status(target, "root", rid) == 404
    assert _move(target, rid, "dora").status_code == 404


def test_mutation_a_decided_assessment_is_open_to_the_admin() -> None:
    mutant = _app_mutant("pending=record.human_decision is None", "pending=True")
    client, target, rid = _setup(mutant)
    assert _decide(client, "bob", rid).status_code == 200
    assert _status(target, "root", rid) == 200


def test_a_hidden_write_route_answers_404_like_a_missing_id() -> None:
    client, _, rid = _setup()
    calls = (
        ("cara", "decision", {"decision": "NO-GO", "rationale": "x"}),
        ("cara", "actual-conditions", {"observations": {}}),
    )
    for who, path, body in calls:
        hidden = client.post(f"/v1/assessments/{rid}/{path}", json=body, headers=AUTH[who])
        gone = client.post(f"/v1/assessments/{'f' * 32}/{path}", json=body, headers=AUTH[who])
        assert hidden.status_code == 404
        assert (hidden.status_code, hidden.json()) == (gone.status_code, gone.json())


@pytest.mark.parametrize(
    ("anchor", "path", "body"),
    [
        (LOAD + ASSIGNED + "        if record.human_decision", "decision", {"decision": "NO-GO"}),
        (
            LOAD + ASSIGNED + "        now = app_state.clock()",
            "actual-conditions",
            {"observations": {}},
        ),
    ],
    ids=["decision", "actual-conditions"],
)
def test_mutation_a_write_route_skips_the_scope_check(
    anchor: str, path: str, body: dict[str, Any]
) -> None:
    mutant = _app_mutant(anchor, _unscoped(anchor))
    _, target, rid = _setup(mutant)
    sent = target.post(f"/v1/assessments/{rid}/{path}", json=body, headers=AUTH["cara"])
    assert sent.status_code == 403  # the mutant tells a hidden id from a missing one


def test_mutation_reassign_skips_the_scope_check() -> None:
    anchor = (
        LOAD + "        if record.human_decision is not None:\n            raise HTTPException("
    )
    mutant = _app_mutant(anchor, _unscoped(anchor))
    client, target, rid = _setup(mutant)
    assert _decide(client, "bob", rid).status_code == 200
    assert _move(client, rid, "cara").status_code == 404  # decided: out of the admin's scope
    assert _move(target, rid, "cara").status_code == 409  # the mutant answers differently


def test_mutation_a_run_in_progress_is_not_scoped_on_the_evidence_path() -> None:
    mutant = _app_mutant(
        RUN_SCOPE + "\n            or run.created_by", "and True\n            or run.created_by"
    )
    client, rid = _with_a_running_run(mutant)
    assert client.get(f"/v1/assessments/{rid}/evidence", headers=AUTH["fred"]).status_code == 409


def test_mutation_a_run_in_progress_is_not_scoped_on_the_status_path() -> None:
    mutant = _app_mutant(
        "(viewer and _in_scope((run.created_by, run.assigned_decider), principal, pending=True))",
        "viewer",
    )
    client, rid = _with_a_running_run(mutant)
    assert client.get(f"/v1/assessments/{rid}", headers=AUTH["fred"]).status_code == 200


def test_mutation_groups_are_ignored_when_reading_one_assessment() -> None:
    mutant = _app_mutant(
        "AssessmentView.of(_scoped(record, principal))", "AssessmentView.of(record)"
    )
    _, target, rid = _setup(mutant)
    assert _status(target, "fred", rid) == 200


def test_mutation_the_evidence_path_is_not_scoped() -> None:
    mutant = _app_mutant(
        "return EvidenceView.of(_load(assessment_id, principal))",
        "return EvidenceView.of(_load(assessment_id))",
    )
    _, target, rid = _setup(mutant)
    assert _status(target, "fred", rid, "/evidence") == 200


def test_mutation_a_group_is_matched_case_sensitively() -> None:
    mutant = mutated_module(
        auth_module, ("        names.add(name.casefold())", "        names.add(name)")
    )
    roles = [r.value for r in frozenset(Role) - {Role.ADMIN}]
    cfg = json.dumps(
        {
            _hash(KEY_A): {"actor": "alice", "roles": roles, "groups": ["North"]},
            _hash(KEY_B): {"actor": "bob", "roles": ["viewer", "decider"], "groups": ["North"]},
            _hash(KEY_ERIN): {"actor": "erin", "roles": ["viewer"], "groups": ["north"]},
        }
    )
    real = ApiKeyAuthenticator(auth_module.parse_api_key_hashes(cfg))
    client, _, rid = _setup(auth=real)
    assert _status(client, "erin", rid) == 200
    lost = ApiKeyAuthenticator(mutant.parse_api_key_hashes(cfg))
    client, _, rid = _setup(auth=lost)
    assert _status(client, "erin", rid) == 404  # the read is lost under the mutant


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
