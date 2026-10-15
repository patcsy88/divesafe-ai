"""Retry keys: a retried create or decision after an unknown outcome must not duplicate a record
or turn a recorded decision into an error, and must never let a different request through."""

from __future__ import annotations

import pytest
from tests.api_polling import create_and_poll
from tests.safety.test_api_human_gate import AUTH_A, AUTH_B, PLAN, _build, _create

pytestmark = pytest.mark.safety

KEY = "retry-key-0123456789"
H = {**AUTH_A, "Idempotency-Key": KEY}


def _count(repo: object) -> int:
    return len(repo._records)  # type: ignore[attr-defined]  # noqa: SLF001


def test_a_retried_create_returns_the_same_record_and_stores_one() -> None:
    client, _, repo = _build()
    first = create_and_poll(client, PLAN, AUTH_A, extra_headers={"Idempotency-Key": KEY})
    again = client.post("/v1/assessments", json=PLAN, headers=H)
    assert first.status_code == 200 and again.status_code == 200
    assert again.headers["idempotent-replay"] == "true"
    assert first.json()["record"]["id"] == again.json()["record"]["id"]
    assert _count(repo) == 1


def test_without_a_key_two_creates_make_two_records() -> None:
    client, _, repo = _build()
    for _ in range(2):
        assert create_and_poll(client, PLAN, AUTH_A).status_code == 200
    assert _count(repo) == 2


def test_the_same_key_with_a_different_plan_is_refused_not_replayed() -> None:
    client, _, repo = _build()
    create_and_poll(client, PLAN, AUTH_A, extra_headers={"Idempotency-Key": KEY})
    other = client.post("/v1/assessments", json={**PLAN, "max_depth_m": 31}, headers=H)
    assert other.status_code == 422 and "different request" in other.json()["detail"]
    assert _count(repo) == 1


def test_one_persons_key_cannot_reach_another_persons_record() -> None:
    client, _, repo = _build()
    mine = create_and_poll(client, PLAN, AUTH_A, extra_headers={"Idempotency-Key": KEY}).json()[
        "record"
    ]["id"]
    theirs = create_and_poll(client, PLAN, AUTH_B, extra_headers={"Idempotency-Key": KEY}).json()[
        "record"
    ]["id"]
    assert mine != theirs and _count(repo) == 2


@pytest.mark.parametrize("bad", ["short", "x" * 129, "has space in it 123", ""])
def test_a_malformed_key_is_refused_and_nothing_is_stored(bad: str) -> None:
    client, _, repo = _build()
    r = client.post("/v1/assessments", json=PLAN, headers={**AUTH_A, "Idempotency-Key": bad})
    assert r.status_code == 422 and _count(repo) == 0


def test_a_retried_identical_decision_is_reported_not_refused() -> None:
    client, _, _ = _build()
    rid = _create(client)["record"]["id"]
    url = f"/v1/assessments/{rid}/decision"
    body = {"decision": "NO-GO", "rationale": "Poor visibility."}
    first = client.post(url, json=body, headers=H)
    again = client.post(url, json=body, headers=H)
    assert first.status_code == 200 and again.status_code == 200
    assert again.headers["idempotent-replay"] == "true"
    assert again.json()["record"]["human_decision"] == first.json()["record"]["human_decision"]


def test_a_different_decision_with_the_same_key_is_still_a_conflict() -> None:
    client, _, _ = _build()
    rid = _create(client)["record"]["id"]
    url = f"/v1/assessments/{rid}/decision"
    client.post(url, json={"decision": "NO-GO", "rationale": "Poor visibility."}, headers=H)
    for body in (
        {"decision": "GO", "rationale": "Changed my mind."},
        {"decision": "NO-GO", "rationale": "A different reason."},
    ):
        assert client.post(url, json=body, headers=H).status_code == 409
    stored = client.get(f"/v1/assessments/{rid}", headers=AUTH_A).json()
    assert stored["record"]["human_decision"]["decision"] == "NO-GO"


def test_an_identical_decision_without_a_key_is_still_a_conflict() -> None:
    client, _, _ = _build()
    rid = _create(client)["record"]["id"]
    url = f"/v1/assessments/{rid}/decision"
    body = {"decision": "NO-GO", "rationale": "Poor visibility."}
    client.post(url, json=body, headers=AUTH_A)
    assert client.post(url, json=body, headers=AUTH_A).status_code == 409


def test_another_person_repeating_the_decision_is_not_a_replay() -> None:
    client, _, _ = _build()
    rid = _create(client)["record"]["id"]
    url = f"/v1/assessments/{rid}/decision"
    body = {"decision": "NO-GO", "rationale": "Poor visibility."}
    client.post(url, json=body, headers=H)
    bob = client.post(url, json=body, headers={**AUTH_B, "Idempotency-Key": KEY})
    assert bob.status_code == 403


def test_a_different_outcome_with_the_same_reason_and_key_is_a_conflict() -> None:
    client, _, _ = _build()
    rid = _create(client)["record"]["id"]
    url = f"/v1/assessments/{rid}/decision"
    reason = "Same words, different outcome."
    client.post(url, json={"decision": "NO-GO", "rationale": reason}, headers=H)
    again = client.post(url, json={"decision": "GO", "rationale": reason}, headers=H)
    assert again.status_code == 409
    stored = client.get(f"/v1/assessments/{rid}", headers=AUTH_A).json()
    assert stored["record"]["human_decision"]["decision"] == "NO-GO"


# --- findings from review ------------------------------------------------------------------------


def test_a_repeat_of_a_create_after_the_window_is_refused_not_served_as_current() -> None:
    from datetime import timedelta

    client, clock, repo = _build()
    create_and_poll(client, PLAN, AUTH_A, extra_headers={"Idempotency-Key": KEY})
    clock.now += timedelta(minutes=11)
    late = client.post("/v1/assessments", json=PLAN, headers=H)
    assert late.status_code == 409 and "nothing was recorded" in late.json()["detail"]
    assert _count(repo) == 1
    clock.now -= timedelta(minutes=2)
    assert client.post("/v1/assessments", json=PLAN, headers=H).status_code == 200


def test_a_create_only_key_cannot_read_a_later_decision_by_repeating_its_create() -> None:
    from fastapi.testclient import TestClient
    from tests.safety.test_api_roles_and_limits import HEADERS, PLAN_D, _client

    from divesafe.api.auth import Role

    client, _ = _client()
    assert isinstance(client, TestClient)
    mine = {**HEADERS[Role.ASSESSOR], "Idempotency-Key": KEY}
    first = create_and_poll(
        client, PLAN_D, HEADERS[Role.ASSESSOR], extra_headers={"Idempotency-Key": KEY}
    )
    rid = first.json()["record"]["id"]
    decided = client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "NO-GO", "rationale": "Secret reasoning."},
        headers=HEADERS[Role.DECIDER],
    )
    assert decided.status_code == 200, decided.text
    again = client.post("/v1/assessments", json=PLAN_D, headers=mine)
    assert again.status_code == 200
    assert again.json()["record"]["human_decision"] is None
    assert client.get(f"/v1/assessments/{rid}", headers=HEADERS[Role.VIEWER]).json()["record"][
        "human_decision"
    ]
    assert "Secret reasoning" not in again.text


def test_a_create_only_key_is_not_told_a_decided_record_is_pending() -> None:
    from tests.safety.test_api_roles_and_limits import HEADERS, PLAN_D, _client

    from divesafe.api.auth import Role

    client, _ = _client()
    mine = {**HEADERS[Role.ASSESSOR], "Idempotency-Key": KEY}
    first = create_and_poll(
        client, PLAN_D, HEADERS[Role.ASSESSOR], extra_headers={"Idempotency-Key": KEY}
    )
    rid = first.json()["record"]["id"]
    # Before any decision the key still cannot see decisions, so it is told that, not "pending".
    assert first.json()["decision_withheld"] is True
    assert first.json()["status"] == "DECISION_WITHHELD"
    assert first.json()["overrides_to_less_severe"] is None
    before = first.json()
    decided = client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "NO-GO", "rationale": "Secret reasoning."},
        headers=HEADERS[Role.DECIDER],
    )
    assert decided.status_code == 200, decided.text
    replay = client.post("/v1/assessments", json=PLAN_D, headers=mine)
    read = client.get(f"/v1/assessments/{rid}", headers=HEADERS[Role.ASSESSOR])
    for body in (replay.json(), read.json()):
        # Exactly what the key saw before the decision: nothing in the reply changes with it.
        assert body == before
        assert body["decision_withheld"] is True
        assert body["status"] == "DECISION_WITHHELD"
        assert body["overrides_to_less_severe"] is None
        assert "Awaiting a human decision" not in body["status_note"]
        assert "not shown here" in body["status_note"]
        assert body["record"]["human_decision"] is None
    seen = client.get(f"/v1/assessments/{rid}", headers=HEADERS[Role.VIEWER]).json()
    assert seen["decision_withheld"] is False and seen["status"] == "DECIDED"


def test_a_tz_equal_start_is_the_same_request() -> None:
    client, _, repo = _build()
    create_and_poll(client, PLAN, AUTH_A, extra_headers={"Idempotency-Key": KEY})
    start = PLAN["planned_start"]
    assert isinstance(start, str)
    from datetime import datetime, timedelta, timezone

    moved = datetime.fromisoformat(start).astimezone(timezone(timedelta(hours=2))).isoformat()
    again = client.post("/v1/assessments", json={**PLAN, "planned_start": moved}, headers=H)
    assert again.status_code == 200 and _count(repo) == 1


def test_the_same_key_with_a_different_decider_is_refused() -> None:
    client, _, repo = _build()
    create_and_poll(
        client, {**PLAN, "decider": "alice"}, AUTH_A, extra_headers={"Idempotency-Key": KEY}
    )
    other = client.post("/v1/assessments", json={**PLAN, "decider": "bob"}, headers=H)
    assert other.status_code == 422 and _count(repo) == 1


def test_a_create_that_finds_the_run_already_complete_at_admission_gets_the_stored_record() -> None:
    client, _, repo = _build()
    first = create_and_poll(client, PLAN, AUTH_A, extra_headers={"Idempotency-Key": KEY}).json()[
        "record"
    ]["id"]
    real_get, calls = repo.get, [0]

    def blind_first_get(assessment_id: str):  # type: ignore[no-untyped-def]
        calls[0] += 1
        return None if calls[0] == 1 else real_get(assessment_id)

    repo.get = blind_first_get  # type: ignore[method-assign]
    raced = client.post("/v1/assessments", json=PLAN, headers=H)
    assert raced.status_code == 200 and raced.json()["record"]["id"] == first


def test_a_racing_identical_decision_is_a_replay_but_a_racing_different_one_is_not() -> None:
    from divesafe.services import ConflictError

    for winner, expected in (("NO-GO", 200), ("INSUFFICIENT EVIDENCE", 409)):
        client, _, repo = _build()
        rid = _create(client)["record"]["id"]
        real_replace = repo.replace

        def lose_race(expected_record, new, w=winner, real=real_replace, r=repo):  # type: ignore[no-untyped-def]
            from divesafe.orchestration import decide

            other = decide(
                expected_record,
                decided_by="alice",
                decision=__import__("divesafe.domain", fromlist=["Recommendation"]).Recommendation(
                    w
                ),
                decided_at=new.human_decision.decided_at,
                rationale="Poor visibility.",
            )
            real(expected_record, other)
            raise ConflictError(expected_record.id)

        repo.replace = lose_race  # type: ignore[method-assign]
        r = client.post(
            f"/v1/assessments/{rid}/decision",
            json={"decision": "NO-GO", "rationale": "Poor visibility."},
            headers=H,
        )
        assert r.status_code == expected, (winner, r.text)


def test_a_decision_replay_after_the_decision_age_limit_is_still_reported() -> None:
    from datetime import timedelta

    client, clock, _ = _build(max_age=timedelta(minutes=30))
    rid = _create(client)["record"]["id"]
    url = f"/v1/assessments/{rid}/decision"
    body = {"decision": "NO-GO", "rationale": "Poor visibility."}
    client.post(url, json=body, headers=H)
    clock.now += timedelta(hours=3)
    assert client.post(url, json=body, headers=H).status_code == 200


def test_a_decision_replay_with_a_different_key_is_the_same_decision() -> None:
    client, _, _ = _build()
    rid = _create(client)["record"]["id"]
    url = f"/v1/assessments/{rid}/decision"
    body = {"decision": "NO-GO", "rationale": "Poor visibility."}
    client.post(url, json=body, headers=H)
    other_key = {**AUTH_A, "Idempotency-Key": "a-different-key-0123456"}
    assert client.post(url, json=body, headers=other_key).status_code == 200
