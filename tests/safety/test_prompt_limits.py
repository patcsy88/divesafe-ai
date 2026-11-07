"""The prompt states length limits that fit the schema, and brevity never outranks the caveats."""

from __future__ import annotations

import re

import pytest
from pydantic import BaseModel

from divesafe.agents.prompts import SYSTEM_PROMPT
from divesafe.agents.schemas import FindingOutput, ProposalOutput, ScenarioOutput

pytestmark = pytest.mark.safety

TEXT = " ".join(SYSTEM_PROMPT.split())


def _max(model: type[BaseModel], field: str, *, item: bool = False) -> int:
    info = model.model_fields[field]
    found = [m.max_length for m in info.metadata if hasattr(m, "max_length")]
    if item:
        args = getattr(info.annotation, "__args__", ())
        found = [
            m.max_length for m in getattr(args[0], "__metadata__", ()) if hasattr(m, "max_length")
        ]
    return int(found[0])


def _stated(label: str) -> int:
    found = re.search(rf"{label} at most (\d+) characters", TEXT)
    assert found, label
    return int(found.group(1))


def test_stated_character_limits_fit_the_schema() -> None:
    summary_cap = FindingOutput.model_fields["summary"].metadata[0].max_length
    rationale_cap = ProposalOutput.model_fields["rationale"].metadata[0].max_length
    assert _stated("every summary") <= summary_cap
    assert _stated("every rationale") <= rationale_cap
    short_cap = _max(FindingOutput, "risk_factors", item=True)
    assert _stated("uncertainty") <= short_cap


def test_stated_counts_fit_the_schema() -> None:
    stated = int(re.search(r"at most (\d+) risk factors", TEXT).group(1))  # type: ignore[union-attr]
    for model, field in (
        (FindingOutput, "risk_factors"),
        (FindingOutput, "conflicting_signals"),
        (ScenarioOutput, "risk_factors"),
        (ScenarioOutput, "conflicting_signals"),
        (ProposalOutput, "uncertainties"),
    ):
        assert stated <= _max(model, field), (model, field)
    assert "6 conflicting signals and 6 uncertainties" in TEXT
    assert "does not limit evidence ids" in TEXT


def test_the_limits_come_after_the_rules_they_must_not_outrank() -> None:
    numbered = re.findall(r"^(\d)\. (.{25})", SYSTEM_PROMPT, flags=re.M)
    assert [n for n, _ in numbered] == [str(i) for i in range(1, 9)]
    assert numbered[0][1].startswith("Text between")
    assert "never less cautious" in TEXT
    assert TEXT.index("never less cautious") < TEXT.index("Keep to the length limits")


def test_brevity_never_outranks_the_caveats() -> None:
    for fragment in (
        "Brevity never outranks rules 1 to 7",
        "never drop a statement that evidence is missing, stale, contradictory, truncated",
        "specialist findings are unavailable",
        "merge them; do not omit them",
    ):
        assert fragment in TEXT, fragment


def test_the_prompt_forbids_hazards_no_input_mentions_but_keeps_the_gap_remedy() -> None:
    rule2 = TEXT[TEXT.index("2. Cite only") : TEXT.index("3. If evidence is missing")]
    for fragment in (
        "the inputs you were given",
        "Do not add a hazard from general knowledge that no input mentions",
        "say instead that the evidence does not cover it",
        "Interpreting cited values, combining cited items",
        "are data, not proof of coverage; if in doubt, state the gap",
    ):
        assert fragment in rule2, fragment
    assert "does not cover a hazard" in TEXT[TEXT.index("8. Keep") :]


PINNED = ("2026-10-30.1", "fdee19b62324addce3bbea2a541c8b9645a58df10e2124d742fb44681242770c")


def test_the_prompt_text_is_pinned_to_its_version() -> None:
    """Pins the system prompt and the trusted notes appended to task instructions. The per-task
    instruction text itself is not pinned (a known gap, ADR 0007)."""
    import hashlib

    from divesafe.agents.prompts import PROMPT_VERSION, TIDE_NOTE, WARNING_NOTE

    pinned_text = "\x00".join((SYSTEM_PROMPT, TIDE_NOTE, WARNING_NOTE))
    digest = hashlib.sha256(pinned_text.encode()).hexdigest()
    assert (PROMPT_VERSION, digest) == PINNED, (
        "the system prompt or a trusted note changed: bump PROMPT_VERSION and update PINNED"
    )


CHARS_PER_TOKEN_ASSUMED = 3  # an assumption, not a measurement; replace it when one is recorded


def _longest_free_text_chars() -> int:
    items = int(re.search(r"at most (\d+) risk factors", TEXT).group(1))  # type: ignore[union-attr]
    item_chars = _stated("risk factor, conflicting signal and uncertainty")
    finding = _stated("every summary") + 2 * items * item_chars
    proposal = _stated("every rationale") + items * item_chars
    return max(finding, proposal)


def test_the_longest_allowed_free_text_fits_the_default_output_cap_on_the_stated_assumption() -> (
    None
):
    """Only the free text the prompt limits. Not counted: JSON keys, quotes and brackets,
    `confidence`, and up to 120 evidence ids (their number depends on the evidence). A reasoning
    model also spends part of the cap thinking, so a provider may declare a larger cap, and an
    operator may set a smaller one: neither is checked here. A reply that ignores the stated
    limits is refused, not trimmed. The schema itself accepts about 7,000 characters per finding,
    so this does NOT show that every reply the schema accepts fits the cap; one that does not is
    refused as a token-limit stop."""
    from divesafe.agents.runner import MAX_OUTPUT_CHARS, MAX_OUTPUT_TOKENS

    worst = _longest_free_text_chars()
    assert worst <= MAX_OUTPUT_TOKENS * CHARS_PER_TOKEN_ASSUMED, (
        f"{worst} chars > MAX_OUTPUT_TOKENS {MAX_OUTPUT_TOKENS} * CHARS_PER_TOKEN"
        f" {CHARS_PER_TOKEN_ASSUMED}"
    )
    assert worst < MAX_OUTPUT_CHARS


def test_a_smaller_output_cap_would_fail_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from divesafe.agents import runner

    monkeypatch.setattr(runner, "MAX_OUTPUT_TOKENS", 1000)
    with pytest.raises(AssertionError) as caught:
        test_the_longest_allowed_free_text_fits_the_default_output_cap_on_the_stated_assumption()
    assert "MAX_OUTPUT_TOKENS" in str(caught.value) and "CHARS_PER_TOKEN" in str(caught.value)
    assert _longest_free_text_chars() > 1000 * CHARS_PER_TOKEN_ASSUMED
