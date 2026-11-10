"""ADR 0022 decisions 6, 9, 12 and 15 in the prompt layer: the knowledge envelope, the trusted
knowledge, withheld and absence wording (outside the untrusted block, in every caller), the
`site_intelligence` withheld-aware finding, and the 20-chunk bound against `MAX_PROMPT_CHARS`.
Each check has a mutation check on a copy of the module."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from typing import Any

import pytest
from tests.agent.scripted_llm import PLAN, fake
from tests.agent.test_knowledge_connector import NOW, SITE, chunk
from tests.safety.kb_fakes import mutated_module

from divesafe.agents import cross_examination as cross_module
from divesafe.agents import prompts as prompts_module
from divesafe.agents import risk_agent as risk_module
from divesafe.agents import specialists as specialists_module
from divesafe.agents import tot as tot_module
from divesafe.agents.prompts import (
    ABSENCE_LINE,
    CLOSE_TAG,
    KNOWLEDGE_NOTE,
    NOT_RETRIEVED,
    OPEN_TAG,
    WITHHELD_FINDING_SUFFIX,
    WITHHELD_FINDING_SUMMARY,
    WITHHELD_KNOWLEDGE_NOTE,
    KnowledgeContext,
    evidence_payload,
    knowledge_wording,
)
from divesafe.agents.runner import MAX_PROMPT_CHARS, AgentError
from divesafe.agents.specialists import NO_EVIDENCE_SUMMARY, SITE_INTELLIGENCE, SPECIALISTS
from divesafe.agents.tot import KINDS
from divesafe.domain import DataCategory, DataKind, EvidenceItem, Finding, Recommendation
from divesafe.rag.items import run_item

pytestmark = pytest.mark.safety

ENVELOPE = {"id", "category", "valid_at", "valid_until", "data_kind", "quality", "scope", "value"}
VALUE = {"text", "section", "document_id", "version", "chunk"}
NEVER = (
    "source",
    "upstream",
    "upstream_known",
    "location",
    "quality_notes",
    "retrieved_at",
    "is_forecast",
    "licence",
    "attribution",
    "origin",
    "author_role",
    "approver_role",
    "content_sha256",
)
WITHHELD = KnowledgeContext(withheld=True, absent=False)
PRESENT = KnowledgeContext(withheld=False, absent=False)
SITE_SPEC = next(s for s in SPECIALISTS if s.name == SITE_INTELLIGENCE)


def _site_item(n: int = 1, text: str | None = None) -> EvidenceItem:
    return run_item(chunk("site-doc", n=n, site=SITE.id, text=text), SITE, NOW)


def _all_sites_item() -> EvidenceItem:
    return run_item(chunk("guide"), SITE, NOW)


def _message(provider: Any) -> str:
    (request,) = provider.requests
    return str(request.messages[1].content)


def _trusted(message: str) -> str:
    return message[: message.index(OPEN_TAG)] + message[message.index(CLOSE_TAG) :]


def _untrusted(message: str) -> str:
    return message[message.index(OPEN_TAG) : message.index(CLOSE_TAG)]


def _peer() -> Finding:
    return Finding(agent="weather", summary="peer", evidence_ids=("kb:site-doc:1:1",))


def _call(module: Any, caller: str, items: list[EvidenceItem], ctx: KnowledgeContext) -> str:
    provider = fake()
    if caller == "specialist":
        asyncio.run(module.run_specialist(provider, SITE_SPEC, PLAN, items, ctx))
    elif caller.startswith("scenario:"):
        kind = next(k for k in KINDS if k.value == caller.split(":")[1])
        asyncio.run(module._one(provider, kind, PLAN, items, [], [], [], ctx))
    elif caller == "risk":
        asyncio.run(
            module.propose(
                provider, PLAN, items, [], [], [], Recommendation.INSUFFICIENT_EVIDENCE, [], ctx
            )
        )
    else:
        asyncio.run(module.cross_examine_one(provider, SITE_SPEC, PLAN, items, [_peer()], ctx))
    return _message(provider)


CALLERS: dict[str, Any] = {
    "specialist": specialists_module,
    **{f"scenario:{k.value}": tot_module for k in KINDS},
    "risk": risk_module,
    "cross_examination": cross_module,
}


# --- decision 6: the envelope ----------------------------------------------------------------


@pytest.mark.parametrize("make", [_site_item, _all_sites_item], ids=["site", "all-sites"])
def test_the_knowledge_envelope_is_exactly_the_allow_list(make: Any) -> None:
    (payload,) = evidence_payload([make()])
    assert set(payload) == ENVELOPE
    assert set(payload["value"]) == VALUE
    assert payload["data_kind"] == "knowledge"


def test_the_scope_marker_tells_site_and_all_sites_apart() -> None:
    site, general = evidence_payload([_site_item(), _all_sites_item()])
    assert (site["scope"], general["scope"]) == ("site", "all_sites")


@pytest.mark.parametrize("caller", sorted(CALLERS))
def test_no_never_sent_field_reaches_any_prompt(caller: str) -> None:
    message = _call(CALLERS[caller], caller, [_site_item(), _all_sites_item()], PRESENT)
    block = _untrusted(message)
    for key in NEVER:
        assert f'"{key}"' not in block, key
    for value in ("Synthetic test licence", "A. Person", "dive_professional", 'kb:site-doc"'):
        assert value not in message, value


@pytest.mark.parametrize(
    "item",
    [
        pytest.param(
            lambda: _site_item().model_copy(update={"id": "Kb:site-doc:1:1"}), id="kb-any-case"
        ),
        pytest.param(
            lambda: _site_item().model_copy(update={"id": "manual:1"}), id="knowledge-kind-only"
        ),
    ],
)
def test_a_reserved_id_or_the_knowledge_kind_alone_gets_the_envelope(item: Any) -> None:
    (payload,) = evidence_payload([item()])
    assert set(payload) == ENVELOPE


def test_a_non_knowledge_item_keeps_the_old_payload() -> None:
    other = _site_item().model_copy(
        update={"id": "x:1", "data_kind": DataKind.NOTICE, "category": DataCategory.WEATHER}
    )
    (payload,) = evidence_payload([other])
    assert "scope" not in payload and "source" in payload


@pytest.mark.parametrize(
    ("edit", "probe"),
    [
        pytest.param(
            ("    if is_knowledge(e):\n        return _knowledge_item(e)\n", ""),
            _site_item,
            id="branch-removed",
        ),
        pytest.param(
            ('"text", "section", "document_id", "version", "chunk")',
             '"text", "section", "document_id", "version", "chunk", "licence")'),
            _site_item,
            id="licence-allowed",
        ),
        pytest.param(
            ("    return reserved or e.data_kind is DataKind.KNOWLEDGE\n",
             "    return reserved\n"),
            lambda: _site_item().model_copy(update={"id": "manual:1"}),
            id="kind-not-checked",
        ),
        pytest.param(
            ("    return reserved or e.data_kind is DataKind.KNOWLEDGE\n",
             "    return e.data_kind is DataKind.KNOWLEDGE\n"),
            lambda: _site_item().model_copy(update={"data_kind": DataKind.NOTICE}),
            id="prefix-not-checked",
        ),
    ],
)  # fmt: skip
def test_mutation_envelope_is_caught(edit: tuple[str, str], probe: Any) -> None:
    mutant = mutated_module(prompts_module, edit)
    (payload,) = mutant.evidence_payload([probe()])
    assert set(payload) != ENVELOPE or set(payload["value"]) != VALUE


# --- decision 9: trusted wording, outside the untrusted block ---------------------------------


@pytest.mark.parametrize("caller", sorted(CALLERS))
@pytest.mark.parametrize(
    ("ctx", "expected", "not_expected"),
    [
        (PRESENT, (KNOWLEDGE_NOTE,), (WITHHELD_KNOWLEDGE_NOTE, ABSENCE_LINE)),
        (WITHHELD, (KNOWLEDGE_NOTE, WITHHELD_KNOWLEDGE_NOTE), (ABSENCE_LINE,)),
        (NOT_RETRIEVED, (KNOWLEDGE_NOTE, ABSENCE_LINE), (WITHHELD_KNOWLEDGE_NOTE,)),
    ],
    ids=["present", "withheld", "absent"],
)
def test_every_caller_carries_the_wording_outside_the_block(
    caller: str, ctx: KnowledgeContext, expected: tuple[str, ...], not_expected: tuple[str, ...]
) -> None:
    message = _call(CALLERS[caller], caller, [_site_item()], ctx)
    for note in expected:
        assert note.strip() in _trusted(message)
        assert note.strip() not in _untrusted(message)
    for note in not_expected:
        assert note.strip() not in message


def test_the_absence_line_is_sent_with_no_knowledge_item_at_all() -> None:
    weather = next(s for s in SPECIALISTS if s.name == "weather")
    item = _site_item().model_copy(
        update={"id": "x:1", "data_kind": DataKind.NOTICE, "category": DataCategory.WEATHER}
    )
    provider = fake()
    asyncio.run(specialists_module.run_specialist(provider, weather, PLAN, [item]))
    message = _trusted(_message(provider))
    assert ABSENCE_LINE.strip() in message
    assert KNOWLEDGE_NOTE.strip() not in message
    assert knowledge_wording([item], PRESENT) == ""


def test_the_wording_holds_the_decision_9_points() -> None:
    for fragment in (
        "typical or past conditions, not this dive window",
        "never supply live values",
        "discount missing, stale, contradictory or adverse live evidence",
        "call conditions acceptable",
        "not reviewed limits",
        "never compare them with a live value",
        "report the disagreement; do not resolve it",
        "all_sites is general guidance",
    ):
        assert fragment in KNOWLEDGE_NOTE, fragment
    assert "not evidence that the site has no hazards" in ABSENCE_LINE
    assert "site context is incomplete" in WITHHELD_KNOWLEDGE_NOTE


def test_the_prediction_focus_refuses_trends_from_documents() -> None:
    spec = next(s for s in SPECIALISTS if s.name == "prediction")
    assert "never take a trend, a forecast or a live value from them" in spec.focus


@pytest.mark.parametrize(
    ("caller", "module", "target"),
    [
        ("specialist", specialists_module, " + knowledge_wording(items, knowledge)"),
        ("scenario:marginal", tot_module, "            + knowledge_wording(evidence, knowledge)\n"),
        ("risk", risk_module, "            + knowledge_wording(evidence, knowledge)\n"),
        (
            "cross_examination",
            cross_module,
            "            + knowledge_wording(evidence, knowledge)\n",
        ),
    ],
)
def test_mutation_wording_dropped_from_a_caller_is_caught(
    caller: str, module: Any, target: str
) -> None:
    mutant = mutated_module(module, (target, "\n" if target.endswith("\n") else ""))
    assert KNOWLEDGE_NOTE.strip() not in _call(mutant, caller, [_site_item()], NOT_RETRIEVED)


@pytest.mark.parametrize(
    ("edit", "ctx"),
    [
        pytest.param(("    if context.withheld:\n", "    if False:\n"), WITHHELD, id="withheld"),
        pytest.param(("    if context.absent:\n", "    if False:\n"), NOT_RETRIEVED, id="absent"),
        pytest.param(
            ("    absent: bool = True\n", "    absent: bool = False\n"), None, id="default"
        ),
        pytest.param(
            ("KNOWLEDGE_NOTE if context.withheld or any(", "KNOWLEDGE_NOTE if any("),
            KnowledgeContext(withheld=True, absent=False),
            id="note-without-items",
        ),
    ],
)
def test_mutation_knowledge_wording_is_caught(
    edit: tuple[str, str], ctx: KnowledgeContext | None
) -> None:
    mutant = mutated_module(prompts_module, edit)
    context = (
        mutant.KnowledgeContext()
        if ctx is None
        else mutant.KnowledgeContext(withheld=ctx.withheld, absent=ctx.absent)
    )
    real = KnowledgeContext() if ctx is None else ctx
    items = [] if edit[0].startswith("KNOWLEDGE_NOTE") else [_site_item()]
    assert mutant.knowledge_wording(items, context) != knowledge_wording(items, real)


# --- decision 15: site_intelligence when knowledge was withheld --------------------------------


def _finding(module: Any, items: list[EvidenceItem], ctx: KnowledgeContext) -> Finding:
    result: Finding = asyncio.run(module.run_specialist(fake(), SITE_SPEC, PLAN, items, ctx))
    return result


def test_all_withheld_gives_the_withheld_finding_not_no_evidence() -> None:
    finding = _finding(specialists_module, [], WITHHELD)
    assert finding.no_evidence is True
    assert finding.summary == WITHHELD_FINDING_SUMMARY
    assert finding.summary != NO_EVIDENCE_SUMMARY
    assert _finding(specialists_module, [], PRESENT).summary == NO_EVIDENCE_SUMMARY


def test_partly_withheld_gives_the_fixed_suffix() -> None:
    finding = _finding(specialists_module, [_site_item()], WITHHELD)
    assert finding.summary.endswith(WITHHELD_FINDING_SUFFIX)
    assert not _finding(specialists_module, [_site_item()], PRESENT).summary.endswith(
        WITHHELD_FINDING_SUFFIX
    )


def test_only_site_intelligence_gets_the_withheld_finding() -> None:
    weather = next(s for s in SPECIALISTS if s.name == "weather")
    finding = asyncio.run(specialists_module.run_specialist(fake(), weather, PLAN, [], WITHHELD))
    assert finding.summary == NO_EVIDENCE_SUMMARY


@pytest.mark.parametrize(
    ("edit", "items"),
    [
        pytest.param(
            ("WITHHELD_FINDING_SUMMARY if withheld_here else NO_EVIDENCE_SUMMARY",
             "NO_EVIDENCE_SUMMARY"),
            [],
            id="summary",
        ),
        pytest.param(
            ("out.summary + (WITHHELD_FINDING_SUFFIX if withheld_here else \"\")", "out.summary"),
            [_site_item()],
            id="suffix",
        ),
        pytest.param(
            ("knowledge.withheld and spec.name == SITE_INTELLIGENCE", "False"),
            [],
            id="never-withheld",
        ),
    ],
)  # fmt: skip
def test_mutation_withheld_finding_is_caught(edit: tuple[str, str], items: Any) -> None:
    mutant = mutated_module(specialists_module, edit)
    real = _finding(specialists_module, items, WITHHELD)
    assert _finding(mutant, items, WITHHELD).summary != real.summary


# --- decision 12: the 20-chunk bound against MAX_PROMPT_CHARS ---------------------------------


def _twenty(text: str, section: str) -> list[EvidenceItem]:
    rows = [replace(chunk(f"d{n}", text=text), section=section) for n in range(1, 21)]
    return [run_item(r, SITE, NOW) for r in rows]


BIG_CALLERS = ["risk", *(f"scenario:{k.value}" for k in KINDS)]


@pytest.mark.parametrize("caller", BIG_CALLERS)
def test_twenty_full_length_ascii_chunks_fit_the_prompt_cap(caller: str) -> None:
    message = _call(CALLERS[caller], caller, _twenty("a" * 1500, "S" * 200), NOT_RETRIEVED)
    assert len(message) <= MAX_PROMPT_CHARS
    assert len(re.findall(r'"kb:d\d+:1:1"', _untrusted(message))) == 20


@pytest.mark.parametrize("caller", BIG_CALLERS)
def test_twenty_full_length_escaped_chunks_fail_the_call_closed(caller: str) -> None:
    """Escaping expands each accented letter to six characters: 20 such chunks are over the cap,
    and the call is refused before it is sent, never trimmed (decision 12)."""
    provider = fake()
    items = _twenty("\u00e9" * 1500, "\u00e9" * 200)
    assert len(json.dumps(evidence_payload(items), ensure_ascii=True)) > MAX_PROMPT_CHARS
    with pytest.raises(AgentError):
        if caller == "risk":
            asyncio.run(
                risk_module.propose(
                    provider, PLAN, items, [], [], [], Recommendation.INSUFFICIENT_EVIDENCE
                )
            )
        else:
            kind = next(k for k in KINDS if k.value == caller.split(":")[1])
            asyncio.run(tot_module._one(provider, kind, PLAN, items, [], [], []))
    assert provider.requests == []
