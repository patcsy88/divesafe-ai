"""ADR 0018 amendment: the adapter sends the model host a looser copy of the output schema (range
and count keywords removed, since some hosts refuse them). Acceptance must not loosen with it: a
reply that breaks a removed limit is still refused by `call_structured`, and only removals are
made, so the host is never asked for a different shape."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from divesafe.agents import AgentError
from divesafe.agents.runner import call_structured
from divesafe.agents.schemas import FindingOutput, ProposalOutput, ScenarioOutput
from divesafe.models.openai_compatible import OpenAICompatibleProvider, host_schema

pytestmark = pytest.mark.safety

OUTPUTS = (FindingOutput, ScenarioOutput, ProposalOutput)
REFUSED_BY_HOST = (
    "maxItems",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
)


def _stream(text: str) -> bytes:
    events = [
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    return ("".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n").encode()


class _Host:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, timeout: float) -> bytes:
        self.bodies.append(json.loads(body))
        return _stream(self.reply)


def _finding(**fields: Any) -> str:
    base: dict[str, Any] = {
        "summary": "s",
        "risk_factors": [],
        "conflicting_signals": [],
        "evidence_ids": ["e1"],
        "confidence": 0.5,
    }
    base.update(fields)
    return json.dumps(base)


def _proposal(**fields: Any) -> str:
    base: dict[str, Any] = {
        "recommendation": "CAUTION",
        "rationale": "r",
        "evidence_ids": ["e1"],
        "uncertainties": [],
    }
    base.update(fields)
    return json.dumps(base)


def _keys(node: object) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


def test_the_valid_reply_is_accepted_through_the_adapter() -> None:
    host = _Host(_finding())
    provider = OpenAICompatibleProvider("m", "http://localhost:8000/v1", transport=host)
    out = asyncio.run(
        call_structured(
            provider,
            task="t",
            user_message="u",
            output=FindingOutput,
            allowed_ids=frozenset({"e1"}),
        )
    )
    assert out.confidence == 0.5
    sent = host.bodies[0]["response_format"]["json_schema"]["schema"]
    assert not _keys(sent) & set(REFUSED_BY_HOST)


@pytest.mark.parametrize(
    ("output", "reply"),
    [
        (FindingOutput, _finding(confidence=1.5)),  # above the removed maximum
        (FindingOutput, _finding(confidence=-0.1)),  # below the removed minimum
        (FindingOutput, _finding(risk_factors=["r"] * 11)),  # over the removed maxItems
        (FindingOutput, _finding(conflicting_signals=["c"] * 11)),
        (FindingOutput, _finding(evidence_ids=["e1"] * 121)),
        (FindingOutput, _finding(evidence_ids=[])),  # minItems 1: kept for the host, checked here
        (ScenarioOutput, _finding(confidence=1.5)),
        (ScenarioOutput, _finding(risk_factors=["r"] * 11)),
        (ProposalOutput, _proposal(uncertainties=["u"] * 11)),
        (ProposalOutput, _proposal(evidence_ids=["e1"] * 121)),
        (ProposalOutput, _proposal(evidence_ids=[])),
    ],
)
def test_a_reply_breaking_a_limit_the_host_was_not_sent_is_still_refused(
    output: Any, reply: str
) -> None:
    host = _Host(reply)
    provider = OpenAICompatibleProvider("m", "http://localhost:8000/v1", transport=host)
    with pytest.raises(AgentError, match="output rejected"):
        asyncio.run(
            call_structured(
                provider,
                task="t",
                user_message="u",
                output=output,
                allowed_ids=frozenset({"e1"}),
            )
        )


@pytest.mark.parametrize("output", OUTPUTS, ids=lambda o: o.__name__)
def test_the_host_copy_only_removes_and_never_touches_the_original(output: Any) -> None:
    schema = output.model_json_schema()
    before = copy.deepcopy(schema)
    sent = host_schema(schema)
    assert schema == before
    assert not _keys(sent) & set(REFUSED_BY_HOST)

    def removed(node: object) -> object:
        if isinstance(node, dict):
            return {
                k: removed(v)
                for k, v in node.items()
                if k not in REFUSED_BY_HOST
                and not (k == "minItems" and not (type(v) is int and 0 <= v <= 1))
            }
        if isinstance(node, list):
            return [removed(v) for v in node]
        return node

    assert sent == removed(before)
    # Everything else the host is asked for stays: types, required fields, no extra fields,
    # string lengths and the recommendation enum.
    for key in ("required", "additionalProperties", "type", "maxLength", "minLength"):
        assert key in _keys(sent) or key not in _keys(before), key


def test_a_property_named_like_a_dropped_keyword_is_kept() -> None:
    schema = {
        "type": "object",
        "properties": {"maximum": {"type": "number", "maximum": 3}},
        "required": ["maximum"],
    }
    assert host_schema(schema) == {
        "type": "object",
        "properties": {"maximum": {"type": "number"}},
        "required": ["maximum"],
    }
