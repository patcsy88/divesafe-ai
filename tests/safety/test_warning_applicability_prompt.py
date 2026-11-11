"""No model is asked, or allowed by its instructions, to decide whether a marine warning applies.

Warning applicability is never decided by code or a model; a human must read the warning
(`WarningNeedsHumanReadingRule`, ADR 0005). The weather specialist's focus text already says so.
The ADR 0025 item 6 run 6 capture showed the gap this closes: the favourable scenario concluded
that the warnings "do not apply". Whether that was right or wrong is not the point; no model may
make that judgement. The scenario, risk and cross-examination prompts now carry the same ban as
trusted wording, outside the untrusted block, whenever a warning item is present.

What this does NOT prove: that a model obeys. Nothing checks reply wording; the ban only removes
the instruction gap. The outcome is protected by the rule and `reconcile`, not by this text.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from tests.agent.scripted_llm import NOW, PLAN, START, WARNINGS, fake, task_of

from divesafe.agents.cross_examination import cross_examine_one
from divesafe.agents.prompts import CLOSE_TAG, OPEN_TAG, warning_note
from divesafe.agents.risk_agent import propose
from divesafe.agents.specialists import SPECIALISTS
from divesafe.agents.tot import KINDS, _one
from divesafe.data import TIOMAN_ISLAND, DataGovMyWarningConnector
from divesafe.domain import DataCategory, Finding, Recommendation

pytestmark = pytest.mark.safety

BAN = "never conclude that a warning applies or does not apply"
HUMAN = "a human must read it"


class _Getter:
    async def get_json(self, url: str, params: object) -> object:
        return WARNINGS


def _items():  # type: ignore[no-untyped-def]
    connector = DataGovMyWarningConnector(_Getter())
    result = asyncio.run(connector.fetch(TIOMAN_ISLAND, START, START + timedelta(hours=2), NOW))
    items = list(result.items)
    assert items and all(e.category is DataCategory.MARINE_WARNINGS for e in items)
    return items


def _user_message(provider) -> str:  # type: ignore[no-untyped-def]
    (request,) = provider.requests
    return str(request.messages[1].content)


def _assert_ban_is_trusted(message: str) -> None:
    start, end = message.index(OPEN_TAG), message.index(CLOSE_TAG)
    outside = message[:start] + message[end:]
    for phrase in (BAN, HUMAN, "the favourable one included"):
        assert phrase in outside, phrase
        assert phrase not in message[start:end], phrase


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.value)
def test_every_scenario_prompt_bans_an_applicability_judgement(kind) -> None:  # type: ignore[no-untyped-def]
    provider = fake()
    asyncio.run(_one(provider, kind, PLAN, _items(), [], [], []))
    assert task_of(provider.requests[0]) == f"scenario:{kind.value}"
    _assert_ban_is_trusted(_user_message(provider))


def test_the_risk_prompt_bans_an_applicability_judgement() -> None:
    provider = fake()
    asyncio.run(propose(provider, PLAN, _items(), [], [], [], Recommendation.INSUFFICIENT_EVIDENCE))
    assert task_of(provider.requests[0]) == "risk_assessment"
    _assert_ban_is_trusted(_user_message(provider))


def test_the_weather_specialist_keeps_its_own_ban() -> None:
    spec = next(s for s in SPECIALISTS if s.name == "weather")
    assert DataCategory.MARINE_WARNINGS in spec.categories
    assert "never decide applicability yourself" in spec.focus


def test_every_specialist_that_reads_warnings_carries_a_ban() -> None:
    readers = [s for s in SPECIALISTS if DataCategory.MARINE_WARNINGS in s.categories]
    assert readers
    assert all("never decide applicability" in s.focus for s in readers)


def test_the_note_appears_only_with_a_warning_item() -> None:
    items = _items()
    assert warning_note([]) == ""
    other = items[0].model_copy(update={"category": DataCategory.WIND})
    assert warning_note([other]) == ""
    assert BAN in warning_note(items)


def _peer(items) -> Finding:  # type: ignore[no-untyped-def]
    """SYNTHETIC peer finding citing a recorded evidence id; test data only."""
    return Finding(agent="ocean_conditions", summary="peer", evidence_ids=(items[0].id,))


def test_the_cross_examination_prompt_bans_an_applicability_judgement() -> None:
    items = _items()
    weather = next(s for s in SPECIALISTS if s.name == "weather")
    provider = fake()
    asyncio.run(cross_examine_one(provider, weather, PLAN, items, [_peer(items)]))
    assert task_of(provider.requests[0]) == "cross_examination:weather"
    _assert_ban_is_trusted(_user_message(provider))


def _wind_only():  # type: ignore[no-untyped-def]
    """The recorded warning items relabelled as wind: no warning item is left."""
    return [e.model_copy(update={"category": DataCategory.WIND}) for e in _items()]


def test_no_prompt_carries_the_ban_without_a_warning_item() -> None:
    items = _wind_only()
    messages = []
    for kind in KINDS:
        provider = fake()
        asyncio.run(_one(provider, kind, PLAN, items, [], [], []))
        messages.append(_user_message(provider))
    provider = fake()
    asyncio.run(propose(provider, PLAN, items, [], [], [], Recommendation.INSUFFICIENT_EVIDENCE))
    messages.append(_user_message(provider))
    weather = next(s for s in SPECIALISTS if s.name == "weather")
    provider = fake()
    asyncio.run(cross_examine_one(provider, weather, PLAN, items, [_peer(items)]))
    messages.append(_user_message(provider))
    assert len(messages) == len(KINDS) + 2
    assert all(BAN not in m for m in messages)


def test_a_hypothetical_stays_allowed_but_never_as_a_finding() -> None:
    # The deteriorating branch may explore a warning that a human finds applies, as a condition.
    note = warning_note(_items())
    assert "describe what would follow if a human finds that a warning applies" in note
    assert "never as a finding" in note
    assert "concludes applicability as unsupported" in note
