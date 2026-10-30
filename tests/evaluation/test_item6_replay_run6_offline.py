"""ADR 0025 item 6: offline replay of the committed run 6 fixture. Offline, deterministic.

What this proves: the recorded connector payloads (including MET Malaysia marine) and the eight
recorded model replies in `tests/fixtures/item6/item6_trace_run6_recorded_2026-10-30.json`, run
through the production `assess_dive` with the production connector classes, the engine built as
the capture builds it and `FakeProvider`, give the recorded outcome and trace codes, use every
recorded reply, and keep the rule that a human must read a marine warning.

The clock is the fixture's own `captured_at_utc`: the instant the live assessment read `now`. Unlike
the run 5 replay, this is the capture moment, not a chosen value. The evidence max age is not in the
fixture; the replay uses 60 minutes (owner decision), as the run 5 replay does.

What this does NOT prove: that the replies' wording is correct. The replies are recorded input. The
`scenario:favourable` reply states that the warnings do not apply, a judgement no component may
make (see tests/fixtures/README.md). No assertion here reads reply text.

No socket is opened: `socket.socket.connect`, `socket.create_connection` and
`socket.getaddrinfo` raise for the whole test.
"""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.evaluation.test_item6_replay_offline import (
    CODE_PATTERN,
    build_replay_connectors,
    replay_reply,
)

from divesafe.api.state import build_engine
from divesafe.config import Settings
from divesafe.data.sites import SITES
from divesafe.domain import AssessmentRecord, DivePlan, Recommendation
from divesafe.models import FakeProvider
from divesafe.orchestration import RunOptions, assess_dive

pytestmark = pytest.mark.safety

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "item6"
    / "item6_trace_run6_recorded_2026-10-30.json"
)
EVIDENCE_MAX_AGE_MINUTES = 60
WARNING_RULE = "policy.marine_warning_needs_human_reading"
MET_MARINE = "met-api-marine-forecast"


def _fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _captured_at(fixture: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(fixture["captured_at_utc"])


def _no_network(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("a network call was attempted during the offline replay")


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)


def run_replay(
    *,
    payloads: dict[str, list[Any]] | None = None,
    replies: dict[str, str] | None = None,
    clock_now: datetime | None = None,
    seen: list[str] | None = None,
) -> AssessmentRecord:
    fixture = _fixture()
    now = _captured_at(fixture)
    later = now if clock_now is None else clock_now
    payload_source = fixture["connector_payloads"] if payloads is None else payloads
    reply_source = fixture["replies"] if replies is None else replies
    requested: list[str] = [] if seen is None else seen
    engine = build_engine(
        Settings(_env_file=None, evidence_max_age_minutes=EVIDENCE_MAX_AGE_MINUTES)
    )
    assert engine is not None
    plan = DivePlan(
        site_id=fixture["site_id"],
        planned_start=datetime.fromisoformat(fixture["planned_start_utc"]),
        planned_duration_minutes=fixture["duration_minutes"],
        max_depth_m=fixture["result"]["max_depth_m"],
    )
    return asyncio.run(
        assess_dive(
            assessment_id="item6-replay-run6",
            plan=plan,
            site=SITES[fixture["site_id"]],
            connectors=build_replay_connectors({k: list(v) for k, v in payload_source.items()}),
            engine=engine,
            now=now,
            provider=FakeProvider(replay_reply(reply_source, requested)),
            clock=lambda: later,
            options=RunOptions(cross_examination=False),
        )
    )


def _codes(record: AssessmentRecord) -> list[str]:
    return sorted(code for s in record.stages for code in s.issues)


def _warning_results(record: AssessmentRecord) -> list[Any]:
    return [r for r in record.rule_results if r.rule_id == WARNING_RULE]


# ---- the faithful replay -----------------------------------------------------------------------


def test_the_fixture_records_a_utc_capture_time_before_the_planned_start() -> None:
    fixture = _fixture()
    captured = _captured_at(fixture)
    assert captured.utcoffset() == timedelta(0)
    assert captured < datetime.fromisoformat(fixture["planned_start_utc"])


def test_the_replay_gives_the_recorded_outcome() -> None:
    fixture = _fixture()
    record = run_replay()
    assert record.status == "PENDING_HUMAN"
    assert record.final_recommendation.value == fixture["result"]["final_recommendation"]
    assert (
        record.deterministic_recommendation.value
        == fixture["result"]["deterministic_recommendation"]
    )
    assert record.final_recommendation is Recommendation.INSUFFICIENT_EVIDENCE
    assert len(record.evidence) == fixture["result"]["evidence_items"]


def test_the_replay_gives_the_recorded_trace_codes_and_stages() -> None:
    fixture = _fixture()
    record = run_replay()
    assert [s.name.value for s in record.stages] == fixture["result"]["stages"]
    assert _codes(record) == sorted(fixture["result"]["stage_issues"])
    for code in _codes(record):
        assert CODE_PATTERN.fullmatch(code), f"not a fixed code: {code!r}"


def test_every_recorded_reply_is_requested_once_and_no_other_label_is() -> None:
    fixture = _fixture()
    seen: list[str] = []
    run_replay(seen=seen)
    assert sorted(seen) == sorted(fixture["replies"])


def test_met_marine_evidence_is_used() -> None:
    record = run_replay()
    assert any(item.source == MET_MARINE for item in record.evidence)


def test_a_human_must_read_the_warnings_whatever_the_scenarios_say() -> None:
    # The recorded favourable scenario says the warnings do not apply. The rule that a human must
    # read a marine warning is deterministic and must still fire: model text never removes it.
    fixture = _fixture()
    assert "scenario:favourable" in fixture["replies"]
    record = run_replay()
    results = _warning_results(record)
    assert results, "the human-reading rule for marine warnings did not fire"
    assert all(r.outcome is Recommendation.INSUFFICIENT_EVIDENCE for r in results)
    # Every recorded warning, dated or not, is cited: none was dropped as "not applicable".
    warning_ids = {e.id for e in record.evidence if e.category.value == "marine_warnings"}
    assert len(warning_ids) >= 2
    assert set(results[0].evidence_ids) == warning_ids


def test_the_recorded_replies_predate_the_current_prompts() -> None:
    # The replay stamps the record with today's PROMPT_VERSION, but the replies came from the
    # version recorded in the fixture. A passing replay is no evidence that today's prompts work.
    from divesafe.agents.prompts import PROMPT_VERSION

    assert _fixture()["result"]["prompt_version"] == "2026-10-17.2"
    assert PROMPT_VERSION != "2026-10-17.2"


# ---- failure tests ------------------------------------------------------------------------------


def test_without_any_warning_the_warning_rule_does_not_fire() -> None:
    # Proves the rule's presence above comes from the recorded warnings, not from the replay setup.
    fixture = _fixture()
    payloads = {k: list(v) for k, v in fixture["connector_payloads"].items()}
    payloads["data-gov-my-weather-warning"] = [[]]
    record = run_replay(payloads=payloads)
    # The connector parsed the empty list as "no active warnings"; it did not fail. Without these
    # two checks a connector failure would also stop the rule firing and pass this test wrongly.
    assert "observe.connector_failed:data-gov-my-weather-warning" not in _codes(record)
    assert [
        e.value.get("kind") for e in record.evidence if e.category.value == "marine_warnings"
    ] == ["no_active_warnings"]
    assert _warning_results(record) == []
    assert record.final_recommendation is not Recommendation.GO


def test_a_missing_met_payload_is_an_issue_and_never_go() -> None:
    fixture = _fixture()
    payloads = {k: list(v) for k, v in fixture["connector_payloads"].items()}
    payloads[MET_MARINE] = []
    record = run_replay(payloads=payloads)
    assert f"observe.connector_failed:{MET_MARINE}" in _codes(record)
    assert record.final_recommendation is not Recommendation.GO
    assert record.deterministic_recommendation is not Recommendation.GO


def test_a_removed_reply_is_a_traced_failure_and_never_go() -> None:
    fixture = _fixture()
    replies = {k: v for k, v in fixture["replies"].items() if k != "scenario:favourable"}
    record = run_replay(replies=replies)
    assert "reason.scenario_failed:favourable" in _codes(record)
    assert record.final_recommendation is not Recommendation.GO


def _stale(record: AssessmentRecord) -> list[str]:
    return sorted(
        r.rule_id
        for r in record.rule_results
        if "No fresh" in r.rationale and r.rule_id.startswith("evidence.required.")
    )


def test_a_later_clock_judges_the_evidence_stale_so_the_pinned_clock_does_work() -> None:
    # `rejudge.rules_added` alone could come from the window starting; the stale-evidence results
    # prove the later clock makes the engine judge the recorded evidence too old.
    assert EVIDENCE_MAX_AGE_MINUTES < 2 * 24 * 60
    fixture = _fixture()
    baseline = run_replay()
    later = run_replay(clock_now=_captured_at(fixture) + timedelta(days=2))
    assert _stale(baseline) == []
    assert "evidence.required.wind" in _stale(later)
    assert "evidence.required.marine_warnings" in _stale(later)
    assert "rejudge.rules_added" in _codes(later)
    assert later.final_recommendation is not Recommendation.GO
