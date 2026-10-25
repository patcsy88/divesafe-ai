"""Rule 8 of the system prompt tells the model shorter limits than the output schemas enforce
(owner decision: the prompt is deliberately STRICTER than the schema, so a model that obeys the
prompt is never rejected). This test reads the numbers out of the real prompt text and the real
schema annotations and asserts each prompt number is <= the schema limit. A prompt number above
its schema limit would invite replies that are then rejected; a prompt whose wording cannot be
parsed fails with a clear message instead of passing silently.

Mutations run on COPIES of `divesafe.agents.prompts` (`mutated_module`), never on src.

EQUIVALENT MUTANTS (not tested): changing a prompt number to another value that is still <= the
schema limit (the test asserts only the upper bound, by owner decision); rewording rule 8 outside
the six numbers in a way the parser still reads; changing the schema's own limits upwards (the
bound only gets looser)."""

from __future__ import annotations

import re
import typing
from dataclasses import dataclass

import pytest
from annotated_types import MaxLen
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic.types import StringConstraints
from tests.safety.kb_fakes import mutated_module

from divesafe.agents import prompts, schemas

pytestmark = pytest.mark.safety

RULE_8 = re.compile(
    r"8\.\s+Keep to the length limits:\s+every summary at most (?P<summary>\d+) characters,\s+"
    r"every rationale at most (?P<rationale>\d+)\s+characters,\s+every risk factor,\s+"
    r"conflicting signal and uncertainty at most (?P<short>\d+)\s+characters,\s+and at\s+"
    r"most (?P<risk_factors>\d+) risk factors,\s+(?P<conflicting_signals>\d+) conflicting "
    r"signals and (?P<uncertainties>\d+) uncertainties"
)


@dataclass(frozen=True)
class PromptLimits:
    summary: int
    rationale: int
    short: int
    risk_factors: int
    conflicting_signals: int
    uncertainties: int


def parse_limits(text: str) -> PromptLimits:
    found = RULE_8.search(text)
    assert found is not None, (
        "cannot parse the length limits of rule 8 in the system prompt; if the wording changed "
        "on purpose, update RULE_8 in this test so the prompt numbers are still checked "
        "against the schema limits"
    )
    return PromptLimits(**{key: int(value) for key, value in found.groupdict().items()})


def string_max(alias: object) -> int:
    """max_length of an Annotated[str, StringConstraints(...)] alias in schemas.py."""
    for meta in typing.get_args(alias)[1:]:
        if isinstance(meta, StringConstraints) and meta.max_length is not None:
            return meta.max_length
    raise AssertionError(f"no max_length found on {alias!r}")


def tuple_max(model: type[BaseModel], field: str) -> int:
    info: FieldInfo = model.model_fields[field]
    for meta in info.metadata:
        if isinstance(meta, MaxLen):
            return meta.max_length
    raise AssertionError(f"no max_length found on {model.__name__}.{field}")


# the (model, field) pairs that rule 8's item counts apply to
TUPLE_FIELDS = {
    "risk_factors": (schemas.FindingOutput, schemas.ScenarioOutput),
    "conflicting_signals": (schemas.FindingOutput, schemas.ScenarioOutput),
    "uncertainties": (schemas.ProposalOutput,),
}


def misses(text: str) -> list[str]:
    """Every prompt limit that is above the schema limit it must stay within."""
    limits = parse_limits(text)
    bad = []
    pairs = [
        ("summary", limits.summary, string_max(schemas.Summary)),
        ("rationale", limits.rationale, string_max(schemas.Rationale)),
        ("short text", limits.short, string_max(schemas.ShortText)),
    ]
    for field, models in TUPLE_FIELDS.items():
        for model in models:
            pairs.append(
                (f"{model.__name__}.{field}", getattr(limits, field), tuple_max(model, field))
            )
    for label, prompt_value, schema_value in pairs:
        if prompt_value > schema_value:
            bad.append(f"{label}: prompt {prompt_value} > schema {schema_value}")
    return bad


# --- the real code -----------------------------------------------------------------------------


def test_the_prompt_limits_are_parsed_from_the_real_prompt() -> None:
    limits = parse_limits(prompts.SYSTEM_PROMPT)
    assert limits.summary > 0
    assert limits.rationale > 0
    assert limits.short > 0
    assert min(limits.risk_factors, limits.conflicting_signals, limits.uncertainties) > 0


def test_every_prompt_limit_is_within_its_schema_limit() -> None:
    found = misses(prompts.SYSTEM_PROMPT)
    assert found == []


def test_the_schema_limits_are_read_from_the_real_annotations() -> None:
    # guards the reader itself: it must see the real, non-trivial schema limits
    assert string_max(schemas.Summary) >= 1
    assert string_max(schemas.Rationale) >= 1
    assert string_max(schemas.ShortText) >= 1
    for field, models in TUPLE_FIELDS.items():
        for model in models:
            assert tuple_max(model, field) >= 1, (model, field)


def test_the_prompt_is_stricter_than_the_schema_for_the_string_limits() -> None:
    # owner decision: stricter on purpose, so the long strings leave headroom
    limits = parse_limits(prompts.SYSTEM_PROMPT)
    assert limits.summary < string_max(schemas.Summary)
    assert limits.rationale < string_max(schemas.Rationale)
    assert limits.short < string_max(schemas.ShortText)


def test_an_unparseable_rule_8_fails_with_a_clear_message() -> None:
    with pytest.raises(AssertionError, match="cannot parse the length limits of rule 8"):
        parse_limits("8. Keep your answers short.")


# --- mutations of the prompt (copies) ----------------------------------------------------------

MUTATIONS = [
    pytest.param("every summary at most 800", "every summary at most 1001", id="summary-above"),
    pytest.param(
        "every rationale at most 1500", "every rationale at most 2001", id="rationale-above"
    ),
    pytest.param("uncertainty at most 200", "uncertainty at most 301", id="short-text-above"),
    pytest.param("at \nmost 6 risk factors", "at \nmost 11 risk factors", id="risk-factors-above"),
    pytest.param("6 conflicting signals", "11 conflicting signals", id="conflicting-signals-above"),
    pytest.param("6 uncertainties", "11 uncertainties", id="uncertainties-above"),
]


@pytest.mark.parametrize(("old", "new"), MUTATIONS)
def test_a_prompt_number_above_the_schema_limit_is_caught(old: str, new: str) -> None:
    source_old = old.replace("\n", "\\\n") if "\n" in old else old
    source_new = new.replace("\n", "\\\n") if "\n" in new else new
    mutant = mutated_module(prompts, (source_old, source_new))
    found = misses(mutant.SYSTEM_PROMPT)
    assert found != []
    assert misses(prompts.SYSTEM_PROMPT) == []


WORDING = [
    pytest.param("Keep to the length limits:", "Be brief:", id="heading-reworded"),
    pytest.param("every summary at most 800 characters", "summaries are short", id="summary-vague"),
    pytest.param("6 risk factors", "a few risk factors", id="count-vague"),
    pytest.param("8. Keep", "9. Keep", id="rule-renumbered"),
]


@pytest.mark.parametrize(("old", "new"), WORDING)
def test_a_wording_change_that_hides_the_numbers_fails_the_parse(old: str, new: str) -> None:
    mutant = mutated_module(prompts, (old, new))
    with pytest.raises(AssertionError, match="cannot parse the length limits of rule 8"):
        misses(mutant.SYSTEM_PROMPT)
