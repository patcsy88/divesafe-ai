"""ADR 0026 Part B step 2: an `admin` moves a pending assessment to another decider."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from tests.api_polling import create_and_poll
from tests.safety.kb_fakes import mutated_module
from tests.safety.test_api_human_gate import AUTH_A, KEY_A, KEY_B, PLAN, _build, _create, _hash

from divesafe.api import app as app_module
from divesafe.api import auth as auth_module
from divesafe.api.auth import ApiKeyAuthenticator, KeyEntry, Principal, Role, parse_api_key_hashes
from divesafe.domain import AssessmentRecord, Reassignment, Recommendation
from divesafe.orchestration import decide
from divesafe.services import InvalidSuccessorError
from divesafe.services.repository import check_successor

pytestmark = pytest.mark.safety

KEY_ROOT, KEY_CARA = "key-for-root-test-only", "key-for-cara-test-only"
AUTH_BOB = {"Authorization": f"Bearer {KEY_B}"}
AUTH_ROOT = {"Authorization": f"Bearer {KEY_ROOT}"}
AUTH_CARA = {"Authorization": f"Bearer {KEY_CARA}"}
DECIDER = frozenset({Role.VIEWER, Role.DECIDER, Role.OVERRIDER})
KEYS = {
    _hash(KEY_A): KeyEntry("alice", frozenset({Role.VIEWER, Role.ASSESSOR, Role.DECIDER})),
    _hash(KEY_B): KeyEntry("bob", DECIDER),
    _hash(KEY_CARA): KeyEntry("cara", DECIDER),
    _hash(KEY_ROOT): KeyEntry("root", frozenset({Role.VIEWER, Role.ADMIN})),
}


def _setup(module: Any = app_module) -> tuple[TestClient, TestClient, Any, str]:
    """The real client, a client on `module`'s app (same state) and a pending id assigned to bob."""
    client, _, repo = _build()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.authenticator = ApiKeyAuthenticator(dict(KEYS))
    rid = _create(client, decider="bob")["record"]["id"]
    return client, TestClient(module.create_app(state)), repo, rid


def _move(client: TestClient, rid: str, to: str, headers: dict[str, str] = AUTH_ROOT) -> Any:
    return client.post(f"/v1/assessments/{rid}/reassign", json={"to_decider": to}, headers=headers)


def _decide(client: TestClient, rid: str, headers: dict[str, str]) -> int:
    body = {"decision": "NO-GO", "rationale": "Too risky for this group."}
    return client.post(f"/v1/assessments/{rid}/decision", json=body, headers=headers).status_code


def test_an_admin_moves_a_pending_assessment_and_the_old_assignee_loses_the_right() -> None:
    client, _, repo, rid = _setup()
    moved = _move(client, rid, "cara")
    assert moved.status_code == 200, moved.text
    stored = repo.get(rid)
    assert stored is not None and stored.assigned_decider == "cara"
    (step,) = stored.reassignments
    assert (step.from_decider, step.to_decider, step.reassigned_by) == ("bob", "cara", "root")
    assert step.key_id == _hash(KEY_ROOT)[:12]
    assert KEY_ROOT not in json.dumps(moved.json())
    assert _decide(client, rid, AUTH_BOB) == 403  # the old assignee, at once
    assert repo.get(rid) == stored  # ... and nothing was recorded
    assert _decide(client, rid, AUTH_CARA) == 200


def test_only_an_admin_may_reassign() -> None:
    client, _, repo, rid = _setup()
    for headers in (AUTH_A, AUTH_BOB, AUTH_CARA):
        assert _move(client, rid, "cara", headers).status_code == 403
    stored = repo.get(rid)
    assert stored is not None and stored.assigned_decider == "bob" and not stored.reassignments


def test_an_admin_cannot_decide_or_report_conditions() -> None:
    client, _, repo, rid = _setup()
    assert _decide(client, rid, AUTH_ROOT) == 403
    stored = repo.get(rid)
    assert stored is not None and stored.human_decision is None


def test_the_new_assignee_must_be_a_known_decider() -> None:
    client, _, repo, rid = _setup()
    for name in ("zed", "root", "  ", "alice-the-unknown"):
        assert _move(client, rid, name).status_code in (422, 409)
    assert _move(client, rid, "zed").status_code == 422
    assert _move(client, rid, "root").status_code == 422  # a key holder without the decider role
    stored = repo.get(rid)
    assert stored is not None and stored.assigned_decider == "bob" and not stored.reassignments


def test_a_decided_assessment_cannot_be_moved() -> None:
    client, _, repo, rid = _setup()
    assert _decide(client, rid, AUTH_BOB) == 200
    before = repo.get(rid)
    assert _move(client, rid, "cara").status_code == 409
    assert repo.get(rid) == before


def test_a_move_to_the_same_person_is_refused() -> None:
    client, _, repo, rid = _setup()
    assert _move(client, rid, "bob").status_code == 409
    assert _move(client, rid, " BOB ").status_code == 409
    stored = repo.get(rid)
    assert stored is not None and not stored.reassignments


def test_moves_are_kept_in_order() -> None:
    client, _, repo, rid = _setup()
    assert _move(client, rid, "cara").status_code == 200
    assert _move(client, rid, "alice").status_code == 200
    stored = repo.get(rid)
    assert stored is not None
    assert [(m.from_decider, m.to_decider) for m in stored.reassignments] == [
        ("bob", "cara"),
        ("cara", "alice"),
    ]


def test_an_unknown_assessment_is_a_404() -> None:
    client, _, _, _ = _setup()
    assert _move(client, "f" * 32, "cara").status_code == 404


class _NoKeyId:
    def authenticate(self, request: Any) -> Any:
        return Principal("root", frozenset({Role.VIEWER, Role.ADMIN}))

    def canonical_decider(self, name: str) -> str | None:
        return name


def _no_key_id_status(module: Any) -> tuple[int, Any]:
    client, _, repo, rid = _setup()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.authenticator = _NoKeyId()
    status = _move(TestClient(module.create_app(state)), rid, "cara").status_code
    stored = repo.get(rid)
    assert stored is not None
    return status, stored


def test_a_move_with_no_key_id_is_refused_and_nothing_is_recorded() -> None:
    status, stored = _no_key_id_status(app_module)
    assert status == 500 and not stored.reassignments and stored.assigned_decider == "bob"


def _config(roles: list[str]) -> str:
    return json.dumps({"a" * 64: {"actor": "root", "roles": roles}})


def test_an_admin_key_must_also_be_a_viewer() -> None:
    with pytest.raises(ValueError, match="admin"):
        parse_api_key_hashes(_config(["admin"]))
    with pytest.raises(ValueError, match="admin"):
        parse_api_key_hashes(_config(["admin", "assessor"]))
    for roles in (["admin", "viewer", "decider"], ["admin", "viewer", "decider", "overrider"]):
        with pytest.raises(ValueError, match="admin"):  # it could take over and decide
            parse_api_key_hashes(_config(roles))
    parsed = parse_api_key_hashes(_config(["admin", "viewer"]))
    assert parsed["a" * 64].roles == frozenset({Role.ADMIN, Role.VIEWER})
    assert Role.DECIDER not in parsed["a" * 64].roles  # admin does not imply decider


# --- mutations: each weakened copy must let the real test's check fail ----------------------------


def test_mutation_any_decider_may_reassign() -> None:
    mutant = mutated_module(
        app_module,
        (
            'REASSIGN = require_role(Role.ADMIN, limit_key="decide")',
            'REASSIGN = require_role(Role.DECIDER, limit_key="decide")',
        ),
    )
    _, target, _, rid = _setup(mutant)
    assert _move(target, rid, "cara", AUTH_BOB).status_code != 403


def test_mutation_the_new_assignee_is_not_checked() -> None:
    mutant = mutated_module(
        app_module,
        (
            "target = app_state.authenticator.canonical_decider(body.to_decider)",
            "target = body.to_decider",
        ),
    )
    _, target, _, rid = _setup(mutant)
    assert _move(target, rid, "zed").status_code != 422


def test_mutation_the_missing_key_id_check_is_removed_from_reassign() -> None:
    mutant = mutated_module(
        app_module,
        (
            "        if not principal.key_id:\n"
            '            logger.error("authenticator set no key id", '
            'extra={"actor": principal.actor})\n'
            '            raise HTTPException(status_code=500, detail="internal error")\n'
            "        target = ",
            "        target = ",
        ),
    )
    with pytest.raises(ValidationError):  # no clean refusal: the domain validator is hit instead
        _no_key_id_status(mutant)


def test_mutation_the_admin_must_also_be_a_viewer_check_is_removed() -> None:
    mutant = mutated_module(
        auth_module,
        ("if Role.ADMIN in role_set and Role.VIEWER not in role_set:", "if False:"),
    )
    assert mutant.parse_api_key_hashes(_config(["admin"]))


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            ("reassigned_by=reassigned_by,", 'reassigned_by="someone",'),
            id="the-mover-is-not-recorded",
        ),
        pytest.param(
            (
                "key_id=key_id,\n    )\n    return _revalidated(",
                'key_id="0" * 12,\n    )\n    return _revalidated(',
            ),
            id="the-mover-key-is-not-recorded",
        ),
    ],
)
def test_mutation_the_move_is_recorded_wrongly(edit: tuple[str, str]) -> None:
    import divesafe.orchestration.human_gate as gate

    mutant = mutated_module(gate, edit)
    client, _, repo, rid = _setup()
    record = repo.get(rid)
    assert record is not None
    moved = mutant.reassign(
        record,
        to_decider="cara",
        reassigned_by="root",
        reassigned_at=record.created_at,
        key_id=_hash(KEY_ROOT)[:12],
    )
    (step,) = moved.reassignments
    assert (step.reassigned_by, step.key_id) != ("root", _hash(KEY_ROOT)[:12])


def test_mutation_the_same_person_check_is_removed() -> None:
    import divesafe.orchestration.human_gate as gate

    mutant = mutated_module(
        gate,
        ("if record.assigned_decider.casefold() == to_decider.strip().casefold():", "if False:"),
    )
    client, _, repo, rid = _setup()
    record = repo.get(rid)
    assert record is not None
    with pytest.raises(ValueError):  # the domain validator is the second line, not the first
        mutant.reassign(
            record,
            to_decider="bob",
            reassigned_by="root",
            reassigned_at=record.created_at,
            key_id="0" * 12,
        )
    with pytest.raises(gate.NotReassignableError):
        gate.reassign(
            record,
            to_decider="bob",
            reassigned_by="root",
            reassigned_at=record.created_at,
            key_id="0" * 12,
        )


def test_mutation_an_admin_may_also_be_a_decider() -> None:
    mutant = mutated_module(
        auth_module,
        ("if Role.ADMIN in role_set and Role.DECIDER in role_set:", "if False:"),
    )
    assert mutant.parse_api_key_hashes(_config(["admin", "viewer", "decider"]))


def test_the_eleventh_move_is_a_409_and_nothing_is_recorded() -> None:
    client, _, repo, rid = _setup()
    for i in range(10):
        assert _move(client, rid, "cara" if i % 2 == 0 else "bob").status_code == 200
    before = repo.get(rid)
    assert before is not None and len(before.reassignments) == 10
    assert _move(client, rid, "cara").status_code == 409
    assert repo.get(rid) == before


def test_a_move_dated_before_the_last_one_is_a_409() -> None:
    client, _, repo, rid = _setup()
    assert _move(client, rid, "cara").status_code == 200
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    real = state.clock
    state.clock = lambda: real() - timedelta(hours=1)
    before = repo.get(rid)
    assert _move(client, rid, "bob").status_code == 409
    assert repo.get(rid) == before


KEY_DAVE = "key-for-dave-test-only"
AUTH_DAVE = {"Authorization": f"Bearer {KEY_DAVE}"}


def test_a_create_only_key_does_not_see_that_the_assessment_was_moved() -> None:
    client, _, repo, _ = _setup()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.authenticator = ApiKeyAuthenticator(
        {**KEYS, _hash(KEY_DAVE): KeyEntry("dave", frozenset({Role.ASSESSOR}))}
    )
    headers = {**AUTH_DAVE, "Idempotency-Key": "d" * 20}
    first = create_and_poll(client, {**PLAN, "decider": "bob"}, headers)
    assert first.status_code == 200, first.text
    rid = first.json()["record"]["id"]
    assert _move(client, rid, "cara").status_code == 200
    seen = client.get(f"/v1/assessments/{rid}", headers=AUTH_DAVE).json()["record"]
    assert seen["reassignments"] == [] and seen["assigned_decider"] == "bob"
    assert "root" not in json.dumps(seen) and _hash(KEY_ROOT)[:12] not in json.dumps(seen)
    retry = client.post("/v1/assessments", json={**PLAN, "decider": "bob"}, headers=headers)
    assert retry.status_code in (200, 202), retry.text  # a legitimate retry, not a 422
    assert retry.json()["record"]["reassignments"] == []


def test_mutation_a_create_only_key_sees_the_moves() -> None:
    from divesafe.api import schemas as schemas_module

    mutant = mutated_module(
        schemas_module,
        (
            '"assigned_decider": record.original_decider,',
            '"assigned_decider": record.assigned_decider,',
        ),
    )
    record = _pending_with_a_move()
    assert mutant.AssessmentView.withheld(record).record.assigned_decider == "cara"  # leaks
    assert schemas_module.AssessmentView.withheld(record).record.assigned_decider == "bob"


def _pending_with_a_move() -> Any:
    client, _, repo, rid = _setup()
    assert _move(client, rid, "cara").status_code == 200
    record = repo.get(rid)
    assert record is not None
    return record


# --- the record's own rules: each is the only thing that stops a record from lying -------------


def _fields(record: Any, **changes: Any) -> dict[str, Any]:
    return {**record.model_dump(), **changes}


def _step(frm: str, to: str, minutes: int, record: Any) -> Reassignment:
    return Reassignment(
        from_decider=frm,
        to_decider=to,
        reassigned_by="root",
        reassigned_at=record.created_at + timedelta(minutes=minutes),
        key_id="0123456789ab",
    )


def test_the_record_refuses_inconsistent_reassignment_history() -> None:
    record = _pending_with_a_move()
    ok = record.reassignments[0]
    assert ok.from_decider == "bob"
    cases = {
        "not a chain": _fields(
            record,
            assigned_decider="dan",
            reassignments=(ok, _step("zed", "dan", 5, record)),
        ),
        "assignee is not where the last move ended": _fields(record, assigned_decider="bob"),
        "out of time order": _fields(
            record,
            assigned_decider="bob",
            reassignments=(ok, _step("cara", "bob", -5, record)),
        ),
        "dated before creation": _fields(
            record,
            reassignments=(_step("bob", "cara", -5, record),),
        ),
    }
    for name, data in cases.items():
        with pytest.raises(ValueError):
            AssessmentRecord.model_validate(data)
            pytest.fail(name)
    with pytest.raises(ValueError):
        _step("bob", " BOB ", 1, record)  # a move to the same person


def test_the_record_refuses_a_move_dated_after_the_decision() -> None:
    record = _pending_with_a_move()
    decided = decide(
        record,
        decided_by="cara",
        decision=Recommendation.NO_GO,
        decided_at=record.reassignments[-1].reassigned_at,
        rationale="Too risky.",
        key_id="0123456789ab",
    )
    late = _step("cara", "dan", 600, record)
    with pytest.raises(ValueError):
        AssessmentRecord.model_validate(
            _fields(decided, assigned_decider="dan", reassignments=(*decided.reassignments, late))
        )


def test_check_successor_alone_refuses_a_move_that_leaves_the_assignee_unchanged() -> None:
    """The in-memory store has only `check_successor` and the record validator between a
    recorded move and the assignee; the validator must hold on its own."""
    client, _, repo, rid = _setup()
    old = repo.get(rid)
    assert old is not None
    forged = old.model_construct(**{**dict(old), "reassignments": (_step("bob", "cara", 1, old),)})
    with pytest.raises(InvalidSuccessorError):
        check_successor(old, forged)


def test_mutation_the_record_validator_is_not_called() -> None:
    from divesafe.domain import models as models_module

    mutant = mutated_module(models_module, ("        self._check_reassignments()\n", ""))
    record = _pending_with_a_move()
    broken = _fields(record, assigned_decider="bob")
    mutant.AssessmentRecord.model_validate(broken)  # the weakened copy accepts the lie
    with pytest.raises(ValueError):
        AssessmentRecord.model_validate(broken)
