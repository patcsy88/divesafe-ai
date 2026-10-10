"""Only the assigned decider may record a decision or actual conditions."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.safety.test_api_human_gate import AUTH_A, AUTH_B, PLAN, _build, _create

from divesafe.api.auth import ApiKeyAuthenticator, KeyEntry, Role
from divesafe.config import Settings
from divesafe.orchestration import NotAssignedDeciderError, check_assigned, decide

pytestmark = pytest.mark.safety

OVERRIDE = {"decision": "GO", "rationale": "I know the site."}
SAFE = {"decision": "INSUFFICIENT EVIDENCE"}


def _stored(client: TestClient, rid: str) -> dict[str, Any]:
    record: dict[str, Any] = client.get(f"/v1/assessments/{rid}", headers=AUTH_A).json()["record"]
    return record


def test_a_different_decider_cannot_decide_or_override_and_nothing_is_recorded() -> None:
    client, _, _ = _build()
    rid = _create(client, decider="alice")["record"]["id"]
    url = f"/v1/assessments/{rid}/decision"
    for body in (SAFE, OVERRIDE):
        refused = client.post(url, json=body, headers=AUTH_B)
        assert refused.status_code == 403
        assert "nothing was recorded" in refused.json()["detail"]
    assert _stored(client, rid)["human_decision"] is None
    assert client.post(url, json=SAFE, headers=AUTH_A).status_code == 200


def test_a_refused_decider_cannot_lock_the_assessment_before_the_right_one_acts() -> None:
    client, _, _ = _build()
    rid = _create(client, decider="alice")["record"]["id"]
    url = f"/v1/assessments/{rid}/decision"
    assert client.post(url, json=OVERRIDE, headers=AUTH_B).status_code == 403
    done = client.post(url, json=SAFE, headers=AUTH_A)
    assert done.status_code == 200
    assert done.json()["record"]["human_decision"]["decided_by"] == "alice"


def test_only_the_assigned_decider_can_report_actual_conditions() -> None:
    client, clock, _ = _build()
    rid = _create(client, decider="alice")["record"]["id"]
    client.post(f"/v1/assessments/{rid}/decision", json=SAFE, headers=AUTH_A)
    clock.now += timedelta(hours=3)
    url = f"/v1/assessments/{rid}/actual-conditions"
    assert client.post(url, json={"observations": {"v": 1}}, headers=AUTH_B).status_code == 403
    assert _stored(client, rid)["actual_conditions"] is None
    assert client.post(url, json={"observations": {"v": 1}}, headers=AUTH_A).status_code == 200


def test_the_assignment_is_recorded_and_cannot_be_supplied_or_changed_later() -> None:
    client, _, _ = _build()
    record = _create(client, decider="bob")["record"]
    assert record["assigned_decider"] == "bob" and record["created_by"] == "alice"
    url = f"/v1/assessments/{record['id']}/decision"
    changed = client.post(url, json={**SAFE, "assigned_decider": "alice"}, headers=AUTH_A)
    assert changed.status_code == 422
    assert client.post(url, json=SAFE, headers=AUTH_A).status_code == 403


def test_the_assignment_defaults_to_the_requester_when_they_can_decide() -> None:
    client, _, _ = _build()
    record = _create(client)["record"]
    assert record["assigned_decider"] == "alice"


def test_an_assignment_to_nobody_known_or_a_non_decider_is_refused() -> None:
    client, _, repo = _build()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.authenticator = ApiKeyAuthenticator(
        {
            _h("a"): KeyEntry("alice", frozenset(Role)),
            _h("v"): KeyEntry("viewer-only", frozenset({Role.VIEWER})),
            _h("s"): KeyEntry("starter", frozenset({Role.ASSESSOR})),
        }
    )
    headers = {"Authorization": "Bearer a"}
    for who in ("mallory", "viewer-only"):
        response = client.post("/v1/assessments", json={**PLAN, "decider": who}, headers=headers)
        assert response.status_code == 422, who
    starter = {"Authorization": "Bearer s"}
    assert client.post("/v1/assessments", json=PLAN, headers=starter).status_code == 422
    assert repo._records == {}  # noqa: SLF001


def test_an_assessor_can_name_a_decider_and_cannot_decide_themselves() -> None:
    client, _, _ = _build()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.authenticator = ApiKeyAuthenticator(
        {
            _h("a"): KeyEntry("alice", frozenset(Role)),
            _h("s"): KeyEntry("starter", frozenset({Role.ASSESSOR})),
        }
    )
    created = client.post(
        "/v1/assessments",
        json={**PLAN, "decider": "Alice"},
        headers={"Authorization": "Bearer s"},
    )
    assert created.status_code == 201
    url = f"/v1/assessments/{created.json()['record']['id']}/decision"
    assert client.post(url, json=SAFE, headers={"Authorization": "Bearer s"}).status_code == 403
    assert client.post(url, json=SAFE, headers={"Authorization": "Bearer a"}).status_code == 200


def test_the_assigned_decider_check_is_case_insensitive_but_exact_otherwise() -> None:
    client, _, _ = _build()
    rid = _create(client, decider="ALICE")["record"]["id"]
    url = f"/v1/assessments/{rid}/decision"
    assert client.post(url, json=SAFE, headers=AUTH_B).status_code == 403
    assert client.post(url, json=SAFE, headers=AUTH_A).status_code == 200


@pytest.mark.parametrize("name", ["alic", "alice2", "ali ce", "\uff41lice", "\u0430lice"])
def test_lookalike_names_are_not_the_assigned_decider(name: str) -> None:
    client, _, _ = _build()
    rid = _create(client, decider="bob")["record"]["id"]
    record = client.app.state.divesafe.repository.get(rid)  # type: ignore[attr-defined]
    with pytest.raises(NotAssignedDeciderError):
        check_assigned(record, name)


def test_a_record_with_no_assignee_can_be_decided_by_nobody() -> None:
    client, _, _ = _build()
    rid = _create(client)["record"]["id"]
    record = client.app.state.divesafe.repository.get(rid)  # type: ignore[attr-defined]
    orphan = record.model_copy(update={"assigned_decider": None})
    for who in ("alice", "bob", ""):
        with pytest.raises(NotAssignedDeciderError):
            decide(
                orphan,
                decided_by=who or "x",
                decision=orphan.final_recommendation,
                decided_at=orphan.created_at,
            )


def test_ownership_is_checked_before_any_other_state_is_revealed() -> None:
    client, clock, _ = _build(max_age=timedelta(minutes=30))
    rid = _create(client, decider="alice")["record"]["id"]
    client.post(f"/v1/assessments/{rid}/decision", json=SAFE, headers=AUTH_A)
    clock.now += timedelta(hours=2)
    url = f"/v1/assessments/{rid}/decision"
    assert client.post(url, json=SAFE, headers=AUTH_B).status_code == 403
    assert client.post(url, json=OVERRIDE, headers=AUTH_B).status_code == 403
    assert client.post(url, json={"decision": "GO"}, headers=AUTH_B).status_code == 403


def test_a_blank_supplied_decider_is_refused_not_defaulted() -> None:
    client, _, _ = _build()
    response = client.post("/v1/assessments", json={**PLAN, "decider": "   "}, headers=AUTH_A)
    assert response.status_code == 422


def test_the_stored_assignee_is_the_configured_name_not_the_typed_spelling() -> None:
    client, _, _ = _build()
    assert _create(client, decider="  ALICE ")["record"]["assigned_decider"] == "alice"


def _h(key: str) -> str:
    import hashlib

    return hashlib.sha256(key.encode()).hexdigest()


# --- production must have keys ---------------------------------------------------------------


@pytest.mark.parametrize("keys", [None, "", "  ", "{}", " { } "])
def test_production_refuses_to_start_without_api_keys(keys: str | None) -> None:
    kwargs: dict[str, Any] = {"api_key_hashes": keys} if keys is not None else {}
    with pytest.raises(ValueError, match="API keys"):
        Settings(environment="production", llm_provider="ollama", **kwargs)


def test_development_may_start_without_keys() -> None:
    Settings(environment="development")


def test_actual_conditions_ownership_is_checked_before_timing_is_revealed() -> None:
    client, _, _ = _build()
    rid = _create(client, decider="alice")["record"]["id"]
    client.post(f"/v1/assessments/{rid}/decision", json=SAFE, headers=AUTH_A)
    url = f"/v1/assessments/{rid}/actual-conditions"
    refused = client.post(url, json={"observations": {"v": 1}}, headers=AUTH_B)
    assert refused.status_code == 403
