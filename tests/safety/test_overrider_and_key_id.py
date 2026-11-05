"""ADR 0026 Part B step 1: the `overrider` role, the key id and `decided_by_creator`."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.safety.kb_fakes import mutated_module
from tests.safety.test_api_human_gate import AUTH_A, KEY_A, KEY_B, _build, _create, _hash

from divesafe.api import app as app_module
from divesafe.api import auth as auth_module
from divesafe.api.auth import ApiKeyAuthenticator, KeyEntry, Role, parse_api_key_hashes
from divesafe.domain import HumanDecision, Recommendation

pytestmark = pytest.mark.safety

DECIDER_ROLES = frozenset({Role.VIEWER, Role.ASSESSOR, Role.DECIDER})


def _client(roles_b: frozenset[Role]) -> tuple[TestClient, Any]:
    client, _, repo = _build()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.authenticator = ApiKeyAuthenticator(
        {
            _hash(KEY_A): KeyEntry("alice", frozenset(Role)),
            _hash(KEY_B): KeyEntry("bob", roles_b),
        }
    )
    return client, repo


AUTH_BOB = {"Authorization": f"Bearer {KEY_B}"}


def _final(client: TestClient, decider: str) -> tuple[str, str]:
    record = _create(client, decider=decider)["record"]
    return record["id"], record["final_recommendation"]


# --- the role ---------------------------------------------------------------------------------


def test_a_decider_without_overrider_cannot_choose_a_less_severe_outcome() -> None:
    client, repo = _client(DECIDER_ROLES)
    rid, final = _final(client, "bob")
    assert final == Recommendation.INSUFFICIENT_EVIDENCE.value  # the interim ruleset
    refused = client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "GO", "rationale": "Local knowledge says it is fine."},
        headers=AUTH_BOB,
    )
    assert refused.status_code == 403
    assert "overrider" in refused.json()["detail"] and "still pending" in refused.json()["detail"]
    stored = repo.get(rid)
    assert stored is not None and stored.human_decision is None  # nothing recorded
    # ... and the same key can still decide properly afterwards (the assessment was not locked)
    ok = client.post(f"/v1/assessments/{rid}/decision", json={"decision": final}, headers=AUTH_BOB)
    assert ok.status_code == 200


def test_a_decider_without_overrider_may_accept_or_choose_a_more_severe_outcome() -> None:
    client, _ = _client(DECIDER_ROLES)
    accept_id, final = _final(client, "bob")
    assert (
        client.post(
            f"/v1/assessments/{accept_id}/decision", json={"decision": final}, headers=AUTH_BOB
        ).status_code
        == 200
    )
    stricter_id, _ = _final(client, "bob")
    done = client.post(
        f"/v1/assessments/{stricter_id}/decision",
        json={"decision": "NO-GO", "rationale": "Too risky for this group."},
        headers=AUTH_BOB,
    )
    assert done.status_code == 200 and done.json()["record"]["human_decision"]["is_override"]


def test_an_overrider_may_choose_a_less_severe_outcome_with_a_rationale() -> None:
    client, _ = _client(DECIDER_ROLES | {Role.OVERRIDER})
    rid, _ = _final(client, "bob")
    url = f"/v1/assessments/{rid}/decision"
    assert client.post(url, json={"decision": "GO"}, headers=AUTH_BOB).status_code == 422
    done = client.post(
        url, json={"decision": "GO", "rationale": "Checked on site."}, headers=AUTH_BOB
    )
    assert done.status_code == 200
    assert done.json()["overrides_to_less_severe"] is True


def test_the_overrider_role_alone_does_not_let_a_key_decide() -> None:
    client, _ = _client(frozenset({Role.VIEWER, Role.OVERRIDER}))
    rid, _ = _final(client, "alice")
    refused = client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "GO", "rationale": "x"},
        headers=AUTH_BOB,
    )
    assert refused.status_code == 403


def test_the_role_check_runs_before_the_refusal_can_reveal_anything_else() -> None:
    """A 403 for a missing role is the same whether or not the assessment is assigned to you."""
    client, _ = _client(DECIDER_ROLES)
    rid, _ = _final(client, "alice")  # assigned to alice, not bob
    wrong_person = client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "GO", "rationale": "x"},
        headers=AUTH_BOB,
    )
    assert wrong_person.status_code == 403
    assert "another decider" in wrong_person.json()["detail"]


# --- configuration ----------------------------------------------------------------------------


def _config(roles: list[str]) -> str:
    return json.dumps({"a" * 64: {"actor": "carol", "roles": roles}})


def test_an_overrider_key_must_also_be_a_decider_and_a_viewer() -> None:
    for roles in (["overrider"], ["overrider", "viewer"]):
        with pytest.raises(ValueError, match="overrider"):
            parse_api_key_hashes(_config(roles))
    with pytest.raises(ValueError):  # a decider must be a viewer too (the older rule)
        parse_api_key_hashes(_config(["overrider", "decider"]))
    parsed = parse_api_key_hashes(_config(["overrider", "decider", "viewer"]))
    assert Role.OVERRIDER in parsed["a" * 64].roles


# --- key id and the self-approval flag --------------------------------------------------------


def test_the_decision_records_the_key_id_and_whether_the_creator_decided() -> None:
    client, _ = _client(DECIDER_ROLES)
    own = _create(client)["record"]["id"]  # alice creates, alice is the assigned decider
    own_done = client.post(
        f"/v1/assessments/{own}/decision",
        json={"decision": "NO-GO", "rationale": "Too risky."},
        headers=AUTH_A,
    ).json()["record"]["human_decision"]
    assert own_done["decided_by_creator"] is True
    assert own_done["key_id"] == _hash(KEY_A)[:12]
    assert KEY_A not in json.dumps(own_done)

    other = _create(client, decider="bob")["record"]["id"]
    other_done = client.post(
        f"/v1/assessments/{other}/decision",
        json={"decision": "NO-GO", "rationale": "Too risky."},
        headers=AUTH_BOB,
    ).json()["record"]["human_decision"]
    assert other_done["decided_by_creator"] is False
    assert other_done["key_id"] == _hash(KEY_B)[:12]


def test_a_caller_cannot_supply_the_key_id_or_the_flag() -> None:
    client, _ = _client(DECIDER_ROLES)
    rid = _create(client)["record"]["id"]
    for extra in ({"key_id": "0" * 12}, {"decided_by_creator": False}):
        response = client.post(
            f"/v1/assessments/{rid}/decision",
            json={"decision": "NO-GO", "rationale": "x", **extra},
            headers=AUTH_A,
        )
        assert response.status_code == 422


def test_dev_authentication_records_dev_as_the_key_id() -> None:
    from starlette.requests import Request

    scope = {"type": "http", "headers": [(b"x-dev-actor", b"dana")]}
    principal = auth_module.DevAuthenticator().authenticate(Request(scope))
    assert principal is not None and principal.key_id == "dev"


def test_the_key_id_must_look_like_a_label_not_a_secret() -> None:
    base: dict[str, Any] = {
        "decided_by": "x",
        "decision": Recommendation.NO_GO,
        "decided_at": "2026-10-08T18:00:00Z",
        "is_override": False,
    }
    assert HumanDecision(**base, key_id="0123456789ab").key_id == "0123456789ab"
    assert HumanDecision(**base).key_id is None  # an old decision
    for bad in ("key-for-alice-test-only", "0123456789abc", "", "ABCDEF123456"):
        with pytest.raises(ValueError):
            HumanDecision(**base, key_id=bad)


def test_an_old_decision_without_the_new_fields_still_loads() -> None:
    old = {
        "decided_by": "x",
        "decision": "NO-GO",
        "decided_at": "2026-10-08T18:00:00Z",
        "is_override": False,
        "override_rationale": None,
    }
    loaded = HumanDecision.model_validate(old)
    assert loaded.key_id is None and loaded.decided_by_creator is None


def test_a_retried_decision_keeps_the_first_key_id() -> None:
    """The idempotent replay returns the stored decision; it does not write a new one."""
    client, _ = _client(DECIDER_ROLES)
    rid = _create(client)["record"]["id"]
    headers = {**AUTH_A, "Idempotency-Key": "k" * 20}
    first = client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "NO-GO", "rationale": "Too risky."},
        headers=headers,
    )
    again = client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "NO-GO", "rationale": "Too risky."},
        headers=headers,
    )
    assert again.headers.get("Idempotent-Replay") == "true"
    assert first.json()["record"]["human_decision"] == again.json()["record"]["human_decision"]


# --- mutations --------------------------------------------------------------------------------


def _gate_misses() -> list[str]:
    bad: list[str] = []
    client, _ = _client(DECIDER_ROLES)
    client.app.state.divesafe.authenticator = ApiKeyAuthenticator(  # type: ignore[attr-defined]
        {
            _hash(KEY_A): KeyEntry("alice", frozenset(Role)),
            _hash(KEY_B): KeyEntry("bob", DECIDER_ROLES),
        }
    )
    rid, _ = _final(client, "bob")
    status = client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "GO", "rationale": "x"},
        headers=AUTH_BOB,
    ).status_code
    if status != 403:
        bad.append("a decider without overrider chose a less severe outcome")
    return bad


def test_the_real_gate_meets_the_check() -> None:
    assert _gate_misses() == []


def test_mutation_the_role_check_is_removed() -> None:
    """The check lives inside `create_app`, so mutate the source text and rebuild the app."""
    mutant = mutated_module(
        app_module,
        (
            "and Role.OVERRIDER not in principal.roles",
            "and Role.OVERRIDER not in (Role.OVERRIDER,)",
        ),
    )
    client, _, _ = _build_with(mutant)
    rid, _ = _final(client, "bob")
    status = client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "GO", "rationale": "x"},
        headers=AUTH_BOB,
    ).status_code
    assert status != 403


def _build_with(module: Any) -> tuple[TestClient, None, None]:
    client, _, _ = _build()
    state = client.app.state.divesafe
    state.authenticator = ApiKeyAuthenticator(
        {
            _hash(KEY_A): KeyEntry("alice", frozenset(Role)),
            _hash(KEY_B): KeyEntry("bob", DECIDER_ROLES),
        }
    )
    app = module.create_app(state)
    return TestClient(app), None, None


def _identity_misses() -> list[str]:
    """Run against whatever `auth_module` / `human_gate` are currently patched in."""
    bad: list[str] = []
    client, _ = _client(DECIDER_ROLES)
    own = _create(client)["record"]["id"]
    done = client.post(
        f"/v1/assessments/{own}/decision",
        json={"decision": "NO-GO", "rationale": "Too risky."},
        headers=AUTH_A,
    ).json()["record"]["human_decision"]
    if done["key_id"] != _hash(KEY_A)[:12]:
        bad.append("the key id is not the first 12 hex characters of the key's hash")
    if done["decided_by_creator"] is not True:
        bad.append("self-approval was not flagged")
    other = _create(client, decider="bob")["record"]["id"]
    done2 = client.post(
        f"/v1/assessments/{other}/decision",
        json={"decision": "NO-GO", "rationale": "Too risky."},
        headers=AUTH_BOB,
    ).json()["record"]["human_decision"]
    if done2["key_id"] != _hash(KEY_B)[:12]:
        bad.append("the second key's id is wrong")
    if done2["decided_by_creator"] is not False:
        bad.append("a different decider was flagged as the creator")
    return bad


def test_the_real_code_meets_the_identity_checks() -> None:
    assert _identity_misses() == []


@pytest.mark.parametrize(
    ("module_name", "edit"),
    [
        pytest.param(
            "divesafe.orchestration.human_gate",
            ("record.created_by.strip().casefold() == decided_by", "False and decided_by"),
            id="self-approval-never-flagged",
        ),
        pytest.param(
            "divesafe.orchestration.human_gate",
            ("record.created_by.strip().casefold() == decided_by.strip().casefold()", "True"),
            id="everyone-flagged-as-creator",
        ),
        pytest.param(
            "divesafe.orchestration.human_gate",
            ("key_id=key_id or None,", "key_id=None,"),
            id="key-id-not-recorded",
        ),
        pytest.param(
            "divesafe.api.auth",
            ("return digest[:12]", "return digest[1:13]"),
            id="key-id-wrong-slice",
        ),
        pytest.param(
            "divesafe.api.auth",
            (
                "Principal(match.actor, match.roles, key_id_of(digest))",
                "Principal(match.actor, match.roles)",
            ),
            id="principal-has-no-key-id",
        ),
    ],
)
def test_mutation_of_key_id_and_self_approval_is_caught(
    module_name: str, edit: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    real = importlib.import_module(module_name)
    mutant = mutated_module(real, edit)
    if module_name.endswith("human_gate"):
        monkeypatch.setattr(app_module, "decide", mutant.decide)
    else:
        monkeypatch.setattr(auth_module, "ApiKeyAuthenticator", mutant.ApiKeyAuthenticator)
        monkeypatch.setattr(auth_module, "key_id_of", mutant.key_id_of)
        monkeypatch.setitem(globals(), "ApiKeyAuthenticator", mutant.ApiKeyAuthenticator)
    try:
        found = _identity_misses()
    except Exception:  # noqa: BLE001
        found = ["the mutant crashed"]
    assert found != []


def test_mutation_the_overrider_start_up_refusal_is_removed() -> None:
    mutant = mutated_module(
        auth_module,
        (
            "if Role.OVERRIDER in role_set and not {Role.DECIDER, Role.VIEWER} <= role_set:",
            "if False:",
        ),
    )
    parsed = mutant.parse_api_key_hashes(_config(["overrider"]))  # the mutant accepts it
    assert Role.OVERRIDER in parsed["a" * 64].roles
    with pytest.raises(ValueError):
        parse_api_key_hashes(_config(["overrider"]))  # the real one refuses


# --- review fixes: the creator flag is validated, and key ids must be distinct -----------------


def _decided_record(created_by: str | None, decided_by: str, flag: bool | None) -> Any:
    from datetime import timedelta

    from tests.agent.scripted_llm import fake, run_pipeline

    from divesafe.domain import AssessmentRecord

    base = run_pipeline(fake())
    human = HumanDecision(
        decided_by=decided_by,
        decision=base.final_recommendation,
        decided_at=base.created_at + timedelta(minutes=1),
        is_override=False,
        key_id="0123456789ab",
        decided_by_creator=flag,
    )
    return AssessmentRecord.model_validate(
        {**base.model_dump(), "created_by": created_by, "human_decision": human.model_dump()}
    )


def test_the_record_refuses_a_creator_flag_that_hides_self_approval() -> None:
    with pytest.raises(ValueError, match="decided_by_creator"):
        _decided_record("alice", "Alice", False)  # the creator decided, case-insensitively
    with pytest.raises(ValueError, match="decided_by_creator"):
        _decided_record("alice", "bob", True)
    with pytest.raises(ValueError, match="decided_by_creator"):
        _decided_record(None, "bob", False)  # nobody to compare with: only None is honest


def test_the_record_accepts_a_true_creator_flag_and_an_unrecorded_one() -> None:
    assert _decided_record("alice", " ALICE ", True).human_decision.decided_by_creator is True
    assert _decided_record("alice", "bob", False).human_decision.decided_by_creator is False
    assert _decided_record("alice", "bob", None).human_decision.decided_by_creator is None
    assert _decided_record(None, "bob", None).human_decision.decided_by_creator is None


def test_mutation_the_creator_flag_check_is_removed() -> None:
    from divesafe.domain import models as models_module

    mutant = mutated_module(
        models_module,
        ('raise ValueError("decided_by_creator must equal (decided_by == created_by)")', "pass"),
    )
    human = HumanDecision(
        decided_by="alice",
        decision=Recommendation.NO_GO,
        decided_at="2026-10-08T18:00:00Z",  # type: ignore[arg-type]
        is_override=False,
        decided_by_creator=False,
    )
    real = _decided_record("bob", "bob", True)  # a valid control record from the real module
    payload = {
        **real.model_dump(),
        "created_by": "alice",
        "human_decision": {
            **human.model_dump(),
            "decision": real.final_recommendation,
            "decided_at": real.human_decision.decided_at,
        },
    }
    payload["human_decision"]["decided_by_creator"] = False
    payload["human_decision"]["is_override"] = False
    assert mutant.AssessmentRecord.model_validate(payload) is not None  # the mutant lets it through
    with pytest.raises(ValueError, match="decided_by_creator"):
        models_module.AssessmentRecord.model_validate(payload)


def test_two_keys_with_the_same_key_id_are_refused_at_start_up() -> None:
    same = "a" * 12
    raw = json.dumps(
        {
            same + "0" * 52: {"actor": "carol", "roles": ["viewer"]},
            same + "1" * 52: {"actor": "dave", "roles": ["viewer"]},
        }
    )
    with pytest.raises(ValueError, match="key id"):
        parse_api_key_hashes(raw)


def test_mutation_the_key_id_clash_check_is_removed() -> None:
    mutant = mutated_module(
        auth_module,
        ("if len(set(labels)) != len(labels):", "if False:"),
    )
    same = "a" * 12
    raw = json.dumps(
        {
            same + "0" * 52: {"actor": "carol", "roles": ["viewer"]},
            same + "1" * 52: {"actor": "dave", "roles": ["viewer"]},
        }
    )
    assert len(mutant.parse_api_key_hashes(raw)) == 2


# --- risk review: the comparison is against the FINAL recommendation --------------------------


def _tightened_client() -> tuple[TestClient, Any]:
    from tests.agent.scripted_llm import scripted

    from divesafe.models import FakeProvider

    client, _, repo = _build(provider=FakeProvider(scripted(recommendation="NO-GO")))
    client.app.state.divesafe.authenticator = ApiKeyAuthenticator(  # type: ignore[attr-defined]
        {
            _hash(KEY_A): KeyEntry("alice", frozenset(Role)),
            _hash(KEY_B): KeyEntry("bob", DECIDER_ROLES),
        }
    )
    return client, repo


def _tightened_status(client: TestClient, decide_with: TestClient | None = None) -> tuple[int, Any]:
    record = _create(client, decider="bob")["record"]
    assert record["final_recommendation"] == "NO-GO"
    assert record["deterministic_recommendation"] == "INSUFFICIENT EVIDENCE"
    response = (decide_with or client).post(
        f"/v1/assessments/{record['id']}/decision",
        json={"decision": "INSUFFICIENT EVIDENCE", "rationale": "Back to the rules' result."},
        headers=AUTH_BOB,
    )
    return response.status_code, record["id"]


def test_going_back_to_the_rules_result_after_the_model_tightened_needs_overrider() -> None:
    client, repo = _tightened_client()
    status, rid = _tightened_status(client)
    assert status == 403
    stored = repo.get(rid)
    assert stored is not None and stored.human_decision is None


def test_mutation_the_comparison_uses_the_rules_result_instead_of_the_final_one() -> None:
    mutant = mutated_module(
        app_module,
        (
            "severity(body.decision) < severity(record.final_recommendation)",
            "severity(body.decision) < severity(record.deterministic_recommendation)",
        ),
    )
    client, _ = _tightened_client()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    status, _ = _tightened_status(client, TestClient(mutant.create_app(state)))
    assert status != 403  # the mutant lets it through, so the real test above has teeth


class _NoKeyId:
    """An authenticator that forgets to set `key_id`."""

    def authenticate(self, request: Any) -> Any:
        from divesafe.api.auth import Principal

        return Principal("bob", DECIDER_ROLES)

    def canonical_decider(self, name: str) -> str | None:
        return name


def _no_key_id_status(module: Any) -> tuple[int, Any]:
    client, repo = _client(DECIDER_ROLES)
    rid, final = _final(client, "bob")
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.authenticator = _NoKeyId()
    target = TestClient(module.create_app(state))
    status = target.post(
        f"/v1/assessments/{rid}/decision", json={"decision": final}, headers=AUTH_BOB
    ).status_code
    stored = repo.get(rid)
    assert stored is not None
    return status, stored.human_decision


def test_a_decision_with_no_key_id_is_refused_and_nothing_is_recorded() -> None:
    status, decision = _no_key_id_status(app_module)
    assert status == 500 and decision is None


def test_mutation_the_missing_key_id_check_is_removed() -> None:
    mutant = mutated_module(
        app_module, ("        if not principal.key_id:\n", "        if False:\n")
    )
    status, decision = _no_key_id_status(mutant)
    assert status == 200 and decision is not None and decision.key_id is None
