"""ADR 0025 item 6: offline replay of the committed run 5 fixture. Offline, deterministic.

What this proves: the recorded connector payloads and model replies in
`tests/fixtures/item6/item6_trace_run5_recorded_2026-10-29.json`, run through the production
`assess_dive` pipeline with the production connector classes, the engine built as the capture builds
it (`api.state.build_engine`) and `FakeProvider`, give the recorded outcome, with the recorded
trace codes, and use every recorded reply.

What this does NOT prove: it is not a match with the live run. The fixture carries no capture time.
`REPLAY_NOW` is a chosen clock, set to the planned start (2026-10-30 03:00 UTC), not the moment the
capture was made. A passing replay proves consistency on recorded data at that clock, nothing more.

No socket is opened: `socket.socket.connect`, `socket.create_connection` and
`socket.getaddrinfo` are replaced with functions that raise for the whole test.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.agent.scripted_llm import task_of

from divesafe.api.state import build_engine
from divesafe.config import Settings
from divesafe.data import (
    DataGovMyForecastConnector,
    DataGovMyWarningConnector,
    MetApiMarineConnector,
    OpenMeteoMarineConnector,
    OpenMeteoWindConnector,
)
from divesafe.data.errors import ConnectorTransportError
from divesafe.data.sites import SITES
from divesafe.domain import AssessmentRecord, DivePlan
from divesafe.models import FakeProvider
from divesafe.orchestration import RunOptions, assess_dive

pytestmark = pytest.mark.safety

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "item6"
    / "item6_trace_run5_recorded_2026-10-29.json"
)
SITE_ID = "my-pahang-pulau-tioman"
# The fixture has no capture time. This is the planned start, chosen as the replay clock (see the
# module docstring): a value of our choosing, not the capture moment.
REPLAY_NOW = datetime(2026, 10, 30, 3, 0, tzinfo=UTC)
# The fixture does not record the evidence max age the capture used. The capture's own settings are
# not in the fixture, so the replay uses 60 minutes, the example value in README.md.
# The +2 day test is only meaningful for a max age under 2880 minutes, so this is asserted too.
EVIDENCE_MAX_AGE_MINUTES = 60
# Issue codes: `stage.kind` or `stage.kind:qualifier`, lowercase, as domain.stages defines them.
CODE_PATTERN = re.compile(r"^[a-z]+\.[a-z_]+(:[a-z0-9_\-]{1,40})?$")


def _fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _no_network(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("a network call was attempted during the offline replay")


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every module-level way to open a connection raises, for every test in this file."""
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)


class ReplayGetter:
    """Serves the recorded payloads for one connector, in order. Exhaustion is a typed connector
    failure (`ConnectorTransportError`), not an AssertionError: `gather_evidence` only catches
    `ConnectorError` and `TimeoutError`, so anything else would abort the run instead of being
    recorded as an issue."""

    def __init__(self, name: str, payloads: list[Any]) -> None:
        self.name = name
        self._payloads = list(payloads)
        self.served = 0

    async def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        if not self._payloads:
            raise ConnectorTransportError(f"replay has no recorded payload left for {self.name}")
        self.served += 1
        return self._payloads.pop(0)


CONNECTOR_CLASSES = (
    OpenMeteoMarineConnector,
    OpenMeteoWindConnector,
    DataGovMyForecastConnector,
    DataGovMyWarningConnector,
    MetApiMarineConnector,
)


def build_replay_connectors(
    payloads: Mapping[str, list[Any]],
) -> list[Any]:
    """The production connector classes, each fed its recorded payloads by connector name. The
    name comes from the class's `name` attribute and is checked against the fixture key."""
    connectors = []
    for cls in CONNECTOR_CLASSES:
        name = cls.name  # the class attribute; the same value the instance reports
        if name not in payloads:
            # MET is built only with its payload; the other four are always present.
            assert cls is MetApiMarineConnector, f"fixture has no payload for {name}"
            continue
        connectors.append(cls(ReplayGetter(name, payloads[name])))
    return connectors


def replay_reply(replies: Mapping[str, str], seen: list[str]) -> Callable[[Any], str]:
    """Returns the recorded text for a task label, recording the label. An unknown label raises
    KeyError, so a request the fixture does not cover fails the test rather than getting a
    default."""

    def reply(request: Any) -> str:
        label = task_of(request)
        seen.append(label)
        return replies[label]

    return reply


def run_replay(
    *,
    payloads: Mapping[str, list[Any]] | None = None,
    replies: Mapping[str, str] | None = None,
    clock_now: datetime = REPLAY_NOW,
    seen: list[str] | None = None,
) -> AssessmentRecord:
    fixture = _fixture()
    payload_source = fixture["connector_payloads"] if payloads is None else payloads
    reply_source = fixture["replies"] if replies is None else replies
    requested: list[str] = [] if seen is None else seen
    connectors = build_replay_connectors({k: list(v) for k, v in payload_source.items()})
    # The engine is built exactly as the capture builds it: api.state.build_engine, with the same
    # max age. No ruleset path, so the placeholders and the interim policy apply.
    settings = Settings(_env_file=None, evidence_max_age_minutes=EVIDENCE_MAX_AGE_MINUTES)
    engine = build_engine(settings)
    assert engine is not None
    plan = DivePlan(
        site_id=SITE_ID,
        planned_start=REPLAY_NOW,
        planned_duration_minutes=fixture["duration_minutes"],
        max_depth_m=18,
    )
    return asyncio.run(
        assess_dive(
            assessment_id="item6-replay",
            plan=plan,
            site=SITES[SITE_ID],
            connectors=connectors,
            engine=engine,
            now=REPLAY_NOW,
            provider=FakeProvider(replay_reply(reply_source, requested)),
            clock=lambda: clock_now,
            options=RunOptions(cross_examination=False),
        )
    )


def _codes_of(record: AssessmentRecord, stage: str) -> list[str]:
    return [code for s in record.stages if s.name.value == stage for code in s.issues]


def _all_codes(record: AssessmentRecord) -> list[str]:
    return [code for s in record.stages for code in s.issues]


# ---- the faithful replay -----------------------------------------------------------------------


def test_the_replay_gives_the_recorded_outcome() -> None:
    fixture = _fixture()
    record = run_replay()
    # The recorded result (fixture `result`) gives the status and both recommendations. A passing
    # run is a PENDING_HUMAN record: nothing is final until a human decides.
    assert record.status == "PENDING_HUMAN"
    assert record.final_recommendation.value == fixture["result"]["final_recommendation"]
    assert (
        record.deterministic_recommendation.value
        == (fixture["result"]["deterministic_recommendation"])
    )
    assert record.final_recommendation.value == "INSUFFICIENT EVIDENCE"
    assert record.deterministic_recommendation.value == "INSUFFICIENT EVIDENCE"


def test_every_recorded_reply_is_requested_and_no_other_label_is() -> None:
    # The replay asks for exactly the labels the fixture recorded. If a reply were never requested,
    # the set would be smaller; a label the fixture does not hold would have raised KeyError.
    fixture = _fixture()
    seen: list[str] = []
    run_replay(seen=seen)
    assert set(seen) == set(fixture["replies"])
    assert len(seen) == len(set(seen)) == len(fixture["replies"])  # one call per label


def test_the_trace_codes_match_the_recorded_stage_codes_as_they_now_are() -> None:
    fixture = _fixture()
    record = run_replay()
    # The stage names and their order are unchanged from the recording.
    assert [s.name.value for s in record.stages] == fixture["result"]["stages"]
    # Observe: every connector answered, so no failure code is expected (the recorded run had none).
    assert _codes_of(record, "observe") == []
    # Retrieve: placeholder, as recorded.
    assert _codes_of(record, "retrieve") == ["retrieve.not_configured"]
    # Assess: the factors without a validated limit are reported as not evaluated, as recorded.
    assert _codes_of(record, "assess") == ["assess.unevaluated_factors"]
    # Reason. The fixture was recorded with the schema short-text limit at 300. Its two rejected
    # replies (a 302-character item on ocean conditions and one on tide current) validate at the
    # current 500-character limit (ADR 0025 item 6), so the Reason stage now has no issues. The
    # committed fixture is not edited; its trace reflects the old limit.
    assert _codes_of(record, "reason") == []
    # Rejudge: the replay pins the clock to the planned start, so the window counts as started
    # (WINDOW_STARTED_RULE). The recorded run had no such code: its clock was earlier than the
    # planned start. The difference comes from the pinned clock, not from a fault in the replay.
    assert _codes_of(record, "rejudge") == [
        "rejudge.window_started",
        "rejudge.rules_added",
    ]


def test_every_issue_code_matches_the_code_pattern_and_no_free_text_is_in_the_trace() -> None:
    # Codes are fixed `stage.kind[:qualifier]` tokens. A free-text reason would contain spaces or
    # sentence punctuation, which the pattern refuses.
    record = run_replay()
    codes = _all_codes(record)
    assert codes, "the replay should produce at least one trace code"
    for code in codes:
        assert CODE_PATTERN.fullmatch(code), f"not a fixed code: {code!r}"
    # The record's own issue strings (agent and evidence) are free text by design; the trace is not.
    for code in codes:
        assert " " not in code


# ---- failure tests: each must fail the replay when broken --------------------------------------


def test_a_removed_recorded_reply_is_a_recorded_failure_and_not_a_silent_pass() -> None:
    # Removing one reply: the label is then not in `replies`, so FakeProvider's lookup raises
    # KeyError inside the call. The pipeline's agent stage catches a provider failure and records a
    # trace code, so the run completes with `reason.specialist_failed:prediction`.
    # The requested-label set then does NOT equal the reply set: `specialist:prediction` was asked
    # for and is not in the removed mapping. Both are asserted, so either check fails the test.
    fixture = _fixture()
    replies = {k: v for k, v in fixture["replies"].items() if k != "specialist:prediction"}
    seen: list[str] = []
    record = run_replay(replies=replies, seen=seen)
    assert record.status == "PENDING_HUMAN"  # the run still completes
    assert "specialist:prediction" in seen  # the label was requested
    assert set(seen) != set(replies)  # so the equality check against the kept replies fails
    assert "reason.specialist_failed:prediction" in _codes_of(record, "reason")
    # And the replay still refuses to give GO: a missing specialist cannot make the result safer.
    assert record.final_recommendation.value == "INSUFFICIENT EVIDENCE"


def test_a_connector_payload_removed_is_recorded_as_an_issue_and_is_not_go() -> None:
    # Remove one recorded payload from one connector. Its getter is exhausted on the first call.
    # The connector raises ConnectorTransportError, gather_evidence records the failure, and the
    # observe stage carries `observe.connector_failed:<name>`. The result must not be GO.
    fixture = _fixture()
    payloads = {k: list(v) for k, v in fixture["connector_payloads"].items()}
    removed = "open-meteo-marine"
    payloads[removed] = []  # the one recorded payload is gone
    record = run_replay(payloads=payloads)
    assert f"observe.connector_failed:{removed}" in _codes_of(record, "observe")
    assert record.evidence_issues  # the issue is also on the record, as text
    assert record.final_recommendation.value != "GO"
    assert record.deterministic_recommendation.value != "GO"


def test_the_clock_two_days_later_judges_evidence_out_of_window_and_the_record_shows_it() -> None:
    # The pinned clock is doing work. Moving the rejudge clock two days later makes the retrieved
    # evidence older than EVIDENCE_MAX_AGE_MINUTES, so the required-evidence rules judge it stale.
    # The assertion is on the rule result, not on the final recommendation, which stays
    # INSUFFICIENT EVIDENCE either way.
    assert EVIDENCE_MAX_AGE_MINUTES < 2 * 24 * 60
    later = REPLAY_NOW + timedelta(days=2)
    record = run_replay(clock_now=later)
    stale = [
        r.rule_id
        for r in record.rule_results
        if "No fresh" in r.rationale and r.rule_id.startswith("evidence.required.")
    ]
    assert stale, "no required-evidence rule judged the evidence stale at the later clock"
    assert "evidence.required.wind" in stale
    # The rejudge stage records that the later result changed (new rules were added).
    assert "rejudge.rules_added" in _codes_of(record, "rejudge")
    # The baseline at the pinned clock does NOT have these stale-evidence rules: the difference is
    # the clock, not the data.
    baseline = run_replay()
    baseline_stale = [
        r.rule_id
        for r in baseline.rule_results
        if "No fresh" in r.rationale and r.rule_id.startswith("evidence.required.")
    ]
    assert baseline_stale == []


# ---- the replay mutation (swap one reply for another, different task) --------------------------


def test_swapping_a_reply_between_tasks_changes_the_recorded_result_and_is_caught() -> None:
    # The weather reply is the recorded weather specialist text. Put the marginal scenario's reply
    # under the weather label: the weather output then fails validation and the finding is lost.
    # Both the trace code and the weather finding's text catch it.
    fixture = _fixture()
    replies = dict(fixture["replies"])
    replies["specialist:weather"], replies["scenario:marginal"] = (
        replies["scenario:marginal"],
        replies["specialist:weather"],
    )
    record = run_replay(replies=replies)
    recorded_weather = json.loads(fixture["replies"]["specialist:weather"])["summary"]
    weather = [f for f in record.findings if f.agent == "weather"]
    assert weather == [] or weather[0].summary != recorded_weather
    assert "reason.specialist_evidence_rejected:weather" in _codes_of(record, "reason")
