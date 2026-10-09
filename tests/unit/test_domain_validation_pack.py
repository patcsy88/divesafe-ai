"""The domain-validation pack stays in sync with the code and never suggests a limit."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from divesafe.domain import THRESHOLD_FACTORS

PACK = Path(__file__).resolve().parents[2] / "docs" / "domain-validation"
QUESTIONNAIRE = (PACK / "questionnaire.md").read_text()


def _sections() -> dict[str, str]:
    parts = re.split(r"^### B\d+\. ", QUESTIONNAIRE, flags=re.MULTILINE)[1:]
    found: dict[str, str] = {}
    for part in parts:
        match = re.match(r".*\(`([a-z_]+)`\)", part.splitlines()[0])
        assert match, part.splitlines()[0]
        found[match.group(1)] = part.split("\n## ", 1)[0]  # a section ends at the next Part
    return found


def test_every_threshold_factor_has_exactly_one_questionnaire_section() -> None:
    assert set(_sections()) == {f.value for f in THRESHOLD_FACTORS}
    assert len(_sections()) == len(THRESHOLD_FACTORS)


@pytest.mark.parametrize("factor", sorted(f.value for f in THRESHOLD_FACTORS))
def test_each_section_is_marked_unvalidated_states_the_data_and_asks_questions(
    factor: str,
) -> None:
    section = _sections()[factor]
    assert "REQUIRES DOMAIN VALIDATION" in section
    assert "**Data we hold:**" in section and "**Questions:**" in section
    assert "Not assessed" in section


@pytest.mark.parametrize("factor", sorted(f.value for f in THRESHOLD_FACTORS))
def test_the_questions_never_suggest_a_number(factor: str) -> None:
    questions = _sections()[factor].split("**Questions:**", 1)[1]
    without_list_markers = re.sub(r"^\d+\. ", "", questions, flags=re.MULTILINE)
    assert not re.search(r"\d", without_list_markers), "a question contains a digit"


def test_the_rule_template_is_blank_and_has_no_numbers() -> None:
    template = (PACK / "rule-template.md").read_text()
    assert not re.search(r"\d", template)
    assert "REQUIRES DOMAIN VALIDATION" in template
    assert "SOURCE" in template and "required" in template


def test_the_pack_states_that_no_limit_is_suggested_and_that_blank_is_safe() -> None:
    assert "No numbers are suggested on purpose" in QUESTIONNAIRE
    readme = (PACK / "README.md").read_text()
    assert "A blank is safe for the software (it stays cautious); a guessed number is not" in readme
    assert "cannot guarantee safety" in (PACK / "signoff-checklist.md").read_text()


def test_the_part_a_decisions_default_to_the_conservative_choice() -> None:
    a1 = QUESTIONNAIRE.split("### A1.", 1)[1].split("### A2.", 1)[0]
    assert "software default today" in a1
    assert "- [ ] Never." in a1


# --- review follow-ups: whole-pack scanning and structure --------------------------------------

_NUMBER_WORDS = (
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty"
    r"|half|quarter"
)
_UNITS = (
    r"m|metre|metres|meter|meters|knot|knots|kt|km|kilometre|kilometres|foot|feet|ft|second"
    r"|seconds|minute|minutes|hour|hours|degree|degrees|percent|mm|cm"
)
_WORD_LIMIT = re.compile(rf"\b({_NUMBER_WORDS})\b[\s-]+({_UNITS})\b", re.IGNORECASE)


def _prose_without_data_facts() -> str:
    """The questionnaire minus the 'Data we hold' paragraphs (which quote provider facts)."""
    text = re.sub(r"\*\*Data we hold:\*\*.*?\n\n", "", QUESTIONNAIRE, flags=re.DOTALL)
    return re.sub(r"^\d+\. ", "", text, flags=re.MULTILINE)


def test_part_a_and_part_c_suggest_no_number() -> None:
    part_a = QUESTIONNAIRE.split("## Part A", 1)[1].split("## Part B", 1)[0]
    part_c = QUESTIONNAIRE.split("## Part C", 1)[1]
    for text in (part_a, part_c):
        cleaned = re.sub(r"\bA\d\b", "", text)
        assert not re.search(r"\d", cleaned)


@pytest.mark.parametrize("name", ["questionnaire.md", "rule-template.md", "signoff-checklist.md"])
def test_no_number_word_next_to_a_unit_anywhere_in_the_prose(name: str) -> None:
    text = _prose_without_data_facts() if name == "questionnaire.md" else (PACK / name).read_text()
    assert _WORD_LIMIT.search(text) is None


def test_the_scanner_would_catch_a_limit_written_in_words() -> None:
    assert _WORD_LIMIT.search("a limit of two metres")
    assert _WORD_LIMIT.search("half a metre".replace("half a", "half"))
    assert _WORD_LIMIT.search("ten knots")
    assert not _WORD_LIMIT.search("one switch for the whole system")


def test_part_a_has_the_five_decisions_and_a_part_c() -> None:
    for label in ("A1", "A2", "A3", "A4", "A5"):
        assert f"### {label}." in QUESTIONNAIRE
    assert "## Part C: not asked, not evaluated" in QUESTIONNAIRE


def test_the_pack_does_not_overstate_what_an_answer_unlocks() -> None:
    readme = (PACK / "README.md").read_text()
    assert "What your answers can and cannot unlock" in readme
    assert "All nine factors need a signed-off rule" in readme
    assert "What the software can apply today" in readme
    assert "does not make any dive safe" in readme
    assert "GO** and **CAUTION" in QUESTIONNAIRE or "`GO` **and** `CAUTION`" in QUESTIONNAIRE


def test_a_second_reviewer_and_an_expiry_are_required_by_the_forms() -> None:
    template = (PACK / "rule-template.md").read_text()
    assert "REQUIRED for any limit that can allow GO or CAUTION" in template
    assert "must not be used after this date" in template
    checklist = (PACK / "signoff-checklist.md").read_text()
    assert "read-back table" in checklist and "**Required**" in checklist


def test_links_to_and_between_the_pack_files_resolve() -> None:
    root = PACK.parents[1]
    linked = [
        (PACK, (PACK / "README.md").read_text()),
        (PACK, QUESTIONNAIRE),
        (root, (root / "README.md").read_text()),
        (root / "docs", (root / "docs" / "risk-model.md").read_text()),
    ]
    for base, text in linked:
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", text):
            if target.startswith(("http", "mailto")):
                continue
            assert (base / target).resolve().exists(), f"broken link: {target}"
