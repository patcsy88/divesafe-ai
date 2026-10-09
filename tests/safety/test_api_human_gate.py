"""The human gate over HTTP: authenticated actors, derived overrides, write-once decisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from divesafe.api.app import create_app
from divesafe.api.auth import ApiKeyAuthenticator
from divesafe.api.state import (
    INTERIM_REQUIRED_CATEGORIES,
    INTERIM_RULESET_VERSION,
    AppState,
)
from divesafe.data import (
    SITES,
    ConnectorTransportError,
    DataGovMyWarningConnector,
    OpenMeteoMarineConnector,
)
from divesafe.risk import EvidencePolicy, RiskRulesEngine, WarningNeedsHumanReadingRule
from divesafe.services import InMemoryAssessmentRepository

pytestmark = pytest.mark.safety

FIXTURES = Path(__file__).parent.parent / "fixtures"
MARINE = json.loads((FIXTURES / "open_meteo_marine_tioman_recorded_2026-10-08.json").read_text())
WARNINGS = json.loads((FIXTURES / "data_gov_my_warning_recorded_2026-10-09.json").read_text())
SITE_ID = "my-pahang-pulau-tioman"
T0 = datetime(2026, 10, 8, 17, 11, tzinfo=UTC)
KEY_A, KEY_B = "key-for-alice-test-only", "key-for-bob-test-only"
AUTH_A = {"Authorization": f"Bearer {KEY_A}"}
AUTH_B = {"Authorization": f"Bearer {KEY_B}"}
PLAN = {
    "site_id": SITE_ID,
    "planned_start": "2026-10-08T18:00:00Z",
    "planned_duration_minutes": 120,
    "max_depth_m": 18,
}


class _Getter:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _build(
    warnings: Any = WARNINGS,
    *,
    max_age: timedelta | None = None,
    engine: bool = True,
    provider: Any = None,
) -> tuple[TestClient, _Clock, InMemoryAssessmentRepository]:
    clock, repo = _Clock(), InMemoryAssessmentRepository()
    rules = RiskRulesEngine(
        [WarningNeedsHumanReadingRule()],
        EvidencePolicy(INTERIM_REQUIRED_CATEGORIES, timedelta(hours=1)),
        INTERIM_RULESET_VERSION,
    )
    state = AppState(
        repository=repo,
        connectors=[
            OpenMeteoMarineConnector(_Getter(MARINE)),
            DataGovMyWarningConnector(_Getter(warnings)),
        ],
        engine=rules if engine else None,
        authenticator=ApiKeyAuthenticator({_hash(KEY_A): "alice", _hash(KEY_B): "bob"}),
        clock=clock,
        sites=SITES,
        decision_max_age=max_age,
        provider=provider,
    )
    return TestClient(create_app(state)), clock, repo


def _create(client: TestClient) -> dict[str, Any]:
    response = client.post("/v1/assessments", json=PLAN, headers=AUTH_A)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# --- authentication ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/assessments"),
        ("get", "/v1/assessments/" + "a" * 32),
        ("post", "/v1/assessments/" + "a" * 32 + "/decision"),
        ("post", "/v1/assessments/" + "a" * 32 + "/actual-conditions"),
    ],
)
def test_every_non_health_endpoint_requires_authentication(method: str, path: str) -> None:
    client, _, _ = _build()
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"X-Dev-Actor": "mallory"}):
        assert getattr(client, method)(path, headers=headers).status_code == 401
    assert client.get("/health").status_code == 200


# --- pipeline over HTTP --------------------------------------------------------------------


def test_assessment_is_stored_pending_human_with_disclaimer_and_attribution() -> None:
    client, _, repo = _build()
    body = _create(client)
    assert body["status"] == "PENDING_HUMAN"
    assert body["record"]["final_recommendation"] == "INSUFFICIENT EVIDENCE"
    assert "not a safety authority" in body["notice"]
    assert any("Open-Meteo" in a for a in body["attributions"])
    assert "not computed" in body["confidence_note"]
    assert repo.get(body["record"]["id"]) is not None


def test_failed_source_is_shown_not_refused() -> None:
    client, _, _ = _build(warnings=ConnectorTransportError("HTTP 503"))
    body = _create(client)
    assert body["record"]["final_recommendation"] == "INSUFFICIENT EVIDENCE"
    assert any("data-gov-my" in i for i in body["record"]["evidence_issues"])


def test_missing_evidence_policy_gives_503_not_a_default() -> None:
    client, _, _ = _build(engine=False)
    response = client.post("/v1/assessments", json=PLAN, headers=AUTH_A)
    assert response.status_code == 503
    assert "EVIDENCE_MAX_AGE" not in response.json()["detail"]  # no config disclosure


def test_unknown_site_and_past_window_are_rejected() -> None:
    client, _, _ = _build()
    assert (
        client.post("/v1/assessments", json={**PLAN, "site_id": "nope"}, headers=AUTH_A).status_code
        == 404
    )
    past = {**PLAN, "planned_start": "2026-10-08T10:00:00Z"}
    assert client.post("/v1/assessments", json=past, headers=AUTH_A).status_code == 422
    assert (
        client.post("/v1/assessments", json={**PLAN, "extra": 1}, headers=AUTH_A).status_code == 422
    )


# --- the human gate ------------------------------------------------------------------------


def test_actor_comes_from_the_credentials_and_cannot_be_supplied() -> None:
    client, _, _ = _build()
    rid = _create(client)["record"]["id"]
    url = f"/v1/assessments/{rid}/decision"
    spoof = {"decision": "INSUFFICIENT EVIDENCE", "decided_by": "carol"}
    assert client.post(url, json=spoof, headers=AUTH_B).status_code == 422
    ok = client.post(url, json={"decision": "INSUFFICIENT EVIDENCE"}, headers=AUTH_B)
    assert ok.status_code == 200
    assert ok.json()["record"]["human_decision"]["decided_by"] == "bob"


def test_override_is_derived_and_needs_a_rationale() -> None:
    client, _, _ = _build()
    rid = _create(client)["record"]["id"]
    url = f"/v1/assessments/{rid}/decision"
    assert client.post(url, json={"decision": "GO"}, headers=AUTH_A).status_code == 422
    assert (
        client.post(url, json={"decision": "GO", "rationale": "  "}, headers=AUTH_A).status_code
        == 422
    )
    done = client.post(
        url, json={"decision": "GO", "rationale": "Leader read the warning."}, headers=AUTH_A
    )
    body = done.json()
    assert done.status_code == 200
    assert body["record"]["human_decision"]["is_override"] is True
    assert body["overrides_to_less_severe"] is True
    assert (
        body["record"]["final_recommendation"] == "INSUFFICIENT EVIDENCE"
    )  # system result untouched


def test_a_decision_cannot_be_replaced() -> None:
    client, _, _ = _build()
    rid = _create(client)["record"]["id"]
    url = f"/v1/assessments/{rid}/decision"
    assert (
        client.post(
            url, json={"decision": "NO-GO", "rationale": "Poor visibility."}, headers=AUTH_A
        ).status_code
        == 200
    )
    again = client.post(
        url, json={"decision": "GO", "rationale": "Changed my mind."}, headers=AUTH_B
    )
    assert again.status_code == 409
    stored = client.get(f"/v1/assessments/{rid}", headers=AUTH_A).json()
    assert stored["record"]["human_decision"]["decided_by"] == "alice"


def test_stale_assessments_cannot_be_decided() -> None:
    client, clock, _ = _build(max_age=timedelta(minutes=30))
    rid = _create(client)["record"]["id"]
    clock.now += timedelta(minutes=31)
    stale = client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "INSUFFICIENT EVIDENCE"},
        headers=AUTH_A,
    )
    assert stale.status_code == 409
    assert "too old" in stale.json()["detail"]


def test_actual_conditions_need_a_decision_and_are_one_shot() -> None:
    client, clock, _ = _build()
    rid = _create(client)["record"]["id"]
    url = f"/v1/assessments/{rid}/actual-conditions"
    clock.now += timedelta(hours=3)
    assert client.post(url, json={"observations": {"vis": "ok"}}, headers=AUTH_A).status_code == 409
    client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "INSUFFICIENT EVIDENCE"},
        headers=AUTH_A,
    )
    clock.now += timedelta(hours=3)
    done = client.post(url, json={"observations": {"vis": "ok"}}, headers=AUTH_B)
    assert done.status_code == 200
    assert done.json()["record"]["actual_conditions"]["reported_by"] == "bob"
    assert client.post(url, json={"observations": {"vis": "x"}}, headers=AUTH_A).status_code == 409


def test_actual_conditions_cannot_be_reported_before_the_dive_starts() -> None:
    client, _, _ = _build()
    rid = _create(client)["record"]["id"]
    client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "INSUFFICIENT EVIDENCE"},
        headers=AUTH_A,
    )
    early = client.post(
        f"/v1/assessments/{rid}/actual-conditions", json={"observations": {"v": 1}}, headers=AUTH_A
    )
    assert early.status_code == 409 and "not started" in early.json()["detail"]


def test_oversized_actual_conditions_are_rejected() -> None:
    client, clock, _ = _build()
    rid = _create(client)["record"]["id"]
    client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "INSUFFICIENT EVIDENCE"},
        headers=AUTH_A,
    )
    clock.now += timedelta(hours=3)
    huge = {"observations": {"note": "x" * 50_000}}
    assert (
        client.post(
            f"/v1/assessments/{rid}/actual-conditions", json=huge, headers=AUTH_A
        ).status_code
        == 422
    )


def test_ids_are_validated_and_unknown_ids_404() -> None:
    client, _, _ = _build()
    assert client.get("/v1/assessments/not-an-id", headers=AUTH_A).status_code == 422
    assert client.get("/v1/assessments/" + "b" * 32, headers=AUTH_A).status_code == 404


# --- review follow-ups: presentation, limits, error mapping ---------------------------------


def test_insufficient_evidence_is_presented_as_not_a_green_light() -> None:
    client, _, _ = _build()
    body = _create(client)
    assert "not a green light" in body["outcome_note"]
    assert "tides" in body["outcome_note"] and "wind" in body["outcome_note"]
    assert "interim and unreviewed" in body["outcome_note"]
    assert "Do not act" in body["status_note"]
    rid = body["record"]["id"]
    done = client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "INSUFFICIENT EVIDENCE"},
        headers=AUTH_A,
    )
    assert "does not change the system's recommendation" in done.json()["status_note"]


def test_client_cannot_supply_recommendation_or_system_fields() -> None:
    client, _, _ = _build()
    for field in ("proposed_recommendation", "final_recommendation", "ruleset_version", "now"):
        response = client.post("/v1/assessments", json={**PLAN, field: "GO"}, headers=AUTH_A)
        assert response.status_code == 422


def test_oversized_bodies_are_refused_before_authentication() -> None:
    client, _, _ = _build()
    big = "x" * 70_000
    unauthenticated = client.post(
        "/v1/assessments", content=big, headers={"content-type": "application/json"}
    )
    assert unauthenticated.status_code == 413


def test_deeply_nested_observations_do_not_cause_a_500() -> None:
    client, clock, _ = _build()
    rid = _create(client)["record"]["id"]
    client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "INSUFFICIENT EVIDENCE"},
        headers=AUTH_A,
    )
    clock.now += timedelta(hours=3)
    nested = "[" * 5000 + "]" * 5000
    response = client.post(
        f"/v1/assessments/{rid}/actual-conditions",
        content='{"observations": {"a": ' + nested + "}}",
        headers={**AUTH_A, "content-type": "application/json"},
    )
    assert response.status_code == 422


def test_already_decided_is_reported_before_staleness() -> None:
    client, clock, _ = _build(max_age=timedelta(minutes=30))
    rid = _create(client)["record"]["id"]
    url = f"/v1/assessments/{rid}/decision"
    client.post(url, json={"decision": "INSUFFICIENT EVIDENCE"}, headers=AUTH_A)
    clock.now += timedelta(hours=2)
    again = client.post(url, json={"decision": "INSUFFICIENT EVIDENCE"}, headers=AUTH_B)
    assert again.status_code == 409 and "already has a decision" in again.json()["detail"]


def test_internal_failures_are_opaque_500s_not_client_errors() -> None:
    class _Broken:
        name = "broken"

        async def fetch(self, *args: object) -> None:
            raise RuntimeError("secret internal detail")

    client, _, _ = _build()
    client.app.state.divesafe.connectors = [_Broken()]  # type: ignore[attr-defined]
    quiet = TestClient(client.app, raise_server_exceptions=False)
    response = quiet.post("/v1/assessments", json=PLAN, headers=AUTH_A)
    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}


def test_health_discloses_non_durable_storage() -> None:
    client, _, _ = _build()
    assert client.get("/health").json()["storage"] == "in-memory (not durable)"


def test_observations_with_excessive_depth_or_count_are_rejected_up_front() -> None:
    client, clock, _ = _build()
    rid = _create(client)["record"]["id"]
    client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "INSUFFICIENT EVIDENCE"},
        headers=AUTH_A,
    )
    clock.now += timedelta(hours=3)
    url = f"/v1/assessments/{rid}/actual-conditions"
    deep = {"observations": {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}}
    wide = {"observations": {f"k{i}": i for i in range(600)}}
    assert client.post(url, json=deep, headers=AUTH_A).status_code == 422
    assert client.post(url, json=wide, headers=AUTH_A).status_code == 422
    stored = client.get(f"/v1/assessments/{rid}", headers=AUTH_A).json()
    assert stored["record"]["actual_conditions"] is None  # nothing half-stored
    ok = client.post(url, json={"observations": {"visibility": {"m": 12}}}, headers=AUTH_A)
    assert ok.status_code == 200


# --- agents over HTTP ----------------------------------------------------------------------


def test_view_says_when_no_llm_was_used() -> None:
    client, _, _ = _build()
    assert "No LLM agents" in _create(client)["agent_note"]


def test_view_labels_agent_output_as_llm_generated_and_advisory() -> None:
    from tests.agent.scripted_llm import scripted

    from divesafe.models import FakeProvider

    client, _, _ = _build(provider=FakeProvider(scripted(recommendation="GO")))
    body = _create(client)
    assert "LLM-generated" in body["agent_note"] and "never relax" in body["agent_note"]
    assert body["record"]["findings"] and len(body["record"]["scenarios"]) == 3
    assert body["record"]["proposed_recommendation"] == "GO"
    assert body["record"]["final_recommendation"] == "INSUFFICIENT EVIDENCE"  # not relaxed
    assert body["record"]["llm_attempted_downgrade"] is True


def test_a_failing_llm_still_returns_a_pending_assessment_with_the_issue_count() -> None:
    from divesafe.models import FakeProvider

    client, _, _ = _build(provider=FakeProvider("garbage"))
    body = _create(client)
    assert body["status"] == "PENDING_HUMAN"
    assert body["record"]["final_recommendation"] == "INSUFFICIENT EVIDENCE"
    assert "failed or were rejected" in body["agent_note"]
