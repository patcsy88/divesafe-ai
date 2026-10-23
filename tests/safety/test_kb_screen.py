"""The loader's text screens (ADR 0022 decisions 6, 9, 13; ADR 0024 A3). These are SCREENS, not
controls (decision 9): paraphrase and splitting across chunks get past them. The known gaps are
strict xfails with their reasons, so closing one turns the test red until the marker is removed.
Mutations run on COPIES of the module source, never on `src`."""

from __future__ import annotations

import unicodedata
from typing import Any

import pytest
from tests.safety.kb_fakes import mutated_module

from divesafe.services import kb_screen

pytestmark = pytest.mark.safety

CAUGHT = (AssertionError, Exception, pytest.fail.Exception)

# --- allowed_text --------------------------------------------------------------------------------

ACCEPTED = [
    "Plain English text.",
    "Numbers 0123456789 and punctuation, ; : ! ? ( ) - / % + = < > $ ^ ` ~",
    "caf\u00e9 \u00fcber na\u00efve",  # Latin letters with diacritics, NFC
    "",
    " ",
    "Quote \u201c text \u201d and dash \u2013 \u2014",  # punctuation
]
REFUSED: dict[str, str] = {
    "zero-width space": "a\u200bb",
    "zero-width joiner": "a\u200db",
    "zero-width non-joiner": "a\u200cb",
    "word joiner": "a\u2060b",
    "byte order mark": "\ufeffa",
    "soft hyphen": "a\u00adb",
    "left-to-right mark": "a\u200eb",
    "right-to-left override": "a\u202eb",
    "bidi isolate": "a\u2066b",
    "tag character": "a\U000e0041b",
    "tag cancel": "\U000e007f",
    "Hangul filler": "a\u3164b",
    "halfwidth Hangul filler": "a\uffa0b",
    "variation selector 16": "a\ufe0fb",
    "variation selector 1": "a\ufe00b",
    "supplementary variation selector": "a\U000e0100b",
    "combining grapheme joiner": "a\u034fb",
    "combining accent alone": "\u0301",
    "Arabic-Indic digit": "\u0663",
    "fullwidth digit": "\uff11",
    "Devanagari digit": "\u0967",
    "emoji": "\U0001f600",
    "copyright sign (So)": "\u00a9",
    "degree-like symbol (So)": "\u2603",
    "NUL": "a\x00b",
    "escape": "a\x1bb",
    "tab": "a\tb",
    "carriage return": "a\rb",
    "next line": "a\u0085b",
    "no-break space": "a\u00a0b",
    "line separator": "a\u2028b",
    "private use": "\ue000",
    "unassigned": "\u0378",
    "noncharacter": "\ufffe",
    "lone surrogate": "\ud800",
    "Cyrillic a": "\u0430",
    "Greek alpha": "\u03b1",
    "CJK ideograph": "\u4e2d",
    "Arabic letter": "\u0627",
    "decomposed e acute (not NFC)": "cafe\u0301",
    "Greek question mark (punctuation, but not NFC)": "\u037e",
}


def check_allowed(module: Any) -> None:
    for text in ACCEPTED:
        assert module.allowed_text(text, newlines=False), repr(text)
    for name, text in REFUSED.items():
        assert not module.allowed_text(text, newlines=False), name
        assert not module.allowed_text(text, newlines=True), name


def test_allowed_text_accepts_ordinary_text_and_refuses_each_dangerous_class() -> None:
    check_allowed(kb_screen)


def test_a_newline_is_allowed_only_when_asked_for() -> None:
    assert kb_screen.allowed_text("a\nb", newlines=True)
    assert not kb_screen.allowed_text("a\nb", newlines=False)
    assert not kb_screen.allowed_text("a\r\nb", newlines=True)


def test_every_refused_example_really_is_in_a_refused_category_or_not_latin() -> None:
    """Guards the table itself: a typo in an example would make a test vacuous."""
    for name, text in REFUSED.items():
        assert text, name
    assert unicodedata.category("\u200b") == "Cf"
    assert unicodedata.category("\ufe0f") == "Mn"
    assert unicodedata.category("\U000e0041") == "Cf"
    assert unicodedata.category("\u3164") == "Lo"
    assert unicodedata.category("\u0663") == "Nd"
    assert unicodedata.category("\U0001f600") == "So"


CATEGORY_LINE = '_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn", "Cs", "Mn", "Mc", "Me"})'
FALLTHROUGH = '    return category.startswith("P") or category in {"Sm", "Sc", "Sk"}'


@pytest.mark.parametrize(
    ("category", "probe"),
    [
        ("Cc", "a\x1bb"),
        ("Cf", "a\u200bb"),
        ("Co", "\ue000"),
        ("Cn", "\u0378"),
        ("Mn", "a\ufe0fb"),
    ],
)
def test_each_refused_category_has_teeth_on_its_own(category: str, probe: str) -> None:
    """The final line of `_char_allowed` is itself an allow-list, so the category list is a second
    wall. Make the first wall permissive, then drop one category: only that category's example
    may get through, proving the entry is not dead."""
    permissive = mutated_module(kb_screen, (FALLTHROUGH, "    return True"))
    without = mutated_module(
        kb_screen,
        (FALLTHROUGH, "    return True"),
        (
            CATEGORY_LINE,
            CATEGORY_LINE.replace(f'"{category}", ', "").replace(f', "{category}"', ""),
        ),
    )
    assert not permissive.allowed_text(probe, newlines=False)
    assert without.allowed_text(probe, newlines=False)


@pytest.mark.parametrize(
    ("old", "new", "probe"),
    [
        (
            '        return unicodedata.name(ch, "").startswith("LATIN ")',
            "        return True",
            "a\u3164b",
        ),
        ('        return "0" <= ch <= "9"', "        return True", "\u0663"),
        (FALLTHROUGH, "    return True", "\U0001f600"),
        (FALLTHROUGH, FALLTHROUGH.replace('"Sk"}', '"Sk", "So"}'), "\U0001f600"),
        (
            '    if unicodedata.normalize("NFC", value) != value or "\\r" in value:\n'
            "        return False\n",
            "",
            "\u037e",
        ),
    ],
)
def test_mutation_a_weaker_allow_list_is_caught(old: str, new: str, probe: str) -> None:
    mutant = mutated_module(kb_screen, (old, new))
    assert not kb_screen.allowed_text(probe, newlines=False)
    assert mutant.allowed_text(probe, newlines=False)
    with pytest.raises(CAUGHT):
        check_allowed(mutant)
    check_allowed(kb_screen)


def test_mutation_a_newline_always_allowed_is_caught() -> None:
    mutant = mutated_module(
        kb_screen,
        (
            '    if ch == "\\n":\n        return newlines',
            '    if ch == "\\n":\n        return True',
        ),
    )
    assert not kb_screen.allowed_text("a\nb", newlines=False)
    assert mutant.allowed_text("a\nb", newlines=False)


# Equivalent mutants, deliberately NOT tested (each is refused by the other wall anyway):
#   - dropping `"\r" in value`: a carriage return is category Cc;
#   - dropping the first NFKC/casefold line of `_fold`: the same fold is applied again at the end.


def test_mutation_the_ascii_space_rule_is_not_widened_to_other_spaces() -> None:
    mutant = mutated_module(
        kb_screen,
        ('    if ch == " ":\n        return True', "    if ch.isspace():\n        return True"),
    )
    assert not kb_screen.allowed_text("a\u00a0b", newlines=False)
    assert mutant.allowed_text("a\u00a0b", newlines=False)


# --- contains_evidence_id ------------------------------------------------------------------------


def check_ids(module: Any) -> None:
    for prefix in ("kb", *kb_screen.CONNECTOR_PREFIXES):
        assert module.contains_evidence_id(f"see {prefix}:abc here"), prefix
        assert module.contains_evidence_id(f"SEE {prefix.upper()}:ABC"), prefix
        assert module.contains_evidence_id(" ".join(prefix) + " :abc"), prefix  # spaced out
    assert module.contains_evidence_id("\uff4b\uff42\uff1aabc")  # fullwidth kb:
    assert module.contains_evidence_id("k\u2028b:abc")  # line separator between letters
    assert module.contains_evidence_id("k  b  :  abc")
    for control in ("\n", "\t", "\r", "\x0b", "\x0c", "\x1c", "\x85"):
        assert module.contains_evidence_id(f"k{control}b:abc"), repr(control)
    assert module.contains_evidence_id("K\u00a0B:abc")  # no-break space
    assert module.contains_evidence_id("\uff2b\uff22\uff1a")  # fullwidth uppercase
    for clean in (
        "kb is short for knowledge base",
        "the open-meteo marine data",
        "weather forecast tomorrow",
        "a time of 10:30",
        "",
        "pkb",  # no colon
    ):
        assert not module.contains_evidence_id(clean), clean


def test_evidence_ids_are_caught_with_case_spaces_and_compatibility_forms() -> None:
    check_ids(kb_screen)


def test_the_connector_prefixes_are_exactly_the_six_connector_sources() -> None:
    assert set(kb_screen.CONNECTOR_PREFIXES) == {
        "data-gov-my-weather-forecast",
        "data-gov-my-weather-warning",
        "met-api-marine-forecast",
        "open-meteo-marine",
        "open-meteo-wind",
        "phn-etide-prediction",
    }
    assert kb_screen.KB_ID_PREFIX == "kb"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            '        ch for ch in text if not (unicodedata.category(ch).startswith("Z")'
            " or ch in _WHITESPACE)\n",
            "        ch for ch in text if not (ch in _WHITESPACE)\n",
        ),
        (
            '        ch for ch in text if not (unicodedata.category(ch).startswith("Z")'
            " or ch in _WHITESPACE)\n",
            '        ch for ch in text if not (unicodedata.category(ch).startswith("Z"))\n',
        ),
        (
            '    markers = tuple(f"{p}:" for p in (KB_ID_PREFIX, *CONNECTOR_PREFIXES))',
            '    markers = tuple(f"{p}:" for p in CONNECTOR_PREFIXES)',
        ),
        (
            '    markers = tuple(f"{p}:" for p in (KB_ID_PREFIX, *CONNECTOR_PREFIXES))',
            '    markers = tuple(f"{p}:" for p in (KB_ID_PREFIX,))',
        ),
        (
            "    return any(marker in folded for marker in markers)",
            "    return all(marker in folded for marker in markers)",
        ),
    ],
)
def test_mutation_a_weaker_id_screen_is_caught(old: str, new: str) -> None:
    mutant = mutated_module(kb_screen, (old, new))
    with pytest.raises(CAUGHT):
        check_ids(mutant)
    check_ids(kb_screen)


# --- KNOWN GAPS (strict xfail: they must fail today; fixing one makes the test red) --------------


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP (decision 9 says screens, not controls): `_fold` strips only Unicode "
    "category Z*, so punctuation between the letters of `kb:` (`k.b:`, `k-b:`, `k_b:`, `k/b:`) "
    "is NOT stripped and the id is not recognised. A paraphrase gets past the screen too.",
)
@pytest.mark.parametrize("text", ["k.b:abc", "k-b:abc", "k_b:abc", "k/b:abc", "k,b:abc"])
def test_punctuation_between_the_letters_of_an_id_is_caught(text: str) -> None:
    assert kb_screen.contains_evidence_id(text)


@pytest.mark.parametrize(
    "separator", ["\n", "\t", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f", "\x85"]
)
@pytest.mark.parametrize("prefix", ["kb", *kb_screen.CONNECTOR_PREFIXES])
def test_a_whitespace_control_inside_an_id_prefix_is_caught(prefix: str, separator: str) -> None:
    text = separator.join(prefix) + ":abc"
    result = kb_screen.contains_evidence_id(text)
    assert result is True


def test_a_newline_between_the_letters_of_an_id_is_caught_in_a_chunk() -> None:
    text = "k\nb:abc"
    assert kb_screen.allowed_text(text, newlines=True)  # a chunk may hold newlines ...
    assert kb_screen.contains_evidence_id(text)  # ... so the id screen must see through them


def test_the_whitespace_set_is_exactly_the_controls_python_calls_whitespace() -> None:
    """Every Cc character str.isspace() accepts must be stripped; one left out is a bypass."""
    cc_space = {chr(c) for c in range(0x20) if chr(c).isspace()} | {"\x85"}
    assert cc_space <= set(kb_screen._WHITESPACE)  # noqa: SLF001
    for ch in sorted(cc_space):
        assert kb_screen.contains_evidence_id(f"k{ch}b:abc"), repr(ch)


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP, GATE FOR THE FIRST PRODUCTION LOAD (owner decision open): a look-alike "
    "of the colon (U+2236 RATIO, U+A789 MODIFIER LETTER COLON, U+0589 ARMENIAN FULL STOP) is not "
    "NFKC-folded to ':' and is a letter/punctuation `allowed_text` accepts, so `kb\u2236abc` "
    "passes both screens. Fixing it turns this red: then drop the marker.",
)
@pytest.mark.parametrize("colon", ["\u2236", "\ua789", "\u0589"])
def test_a_lookalike_colon_after_an_id_prefix_is_caught(colon: str) -> None:
    text = f"kb{colon}abc"
    assert kb_screen.allowed_text(text, newlines=True)
    assert kb_screen.contains_evidence_id(text)


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP, GATE FOR THE FIRST PRODUCTION LOAD (owner decision open): U+2010 HYPHEN "
    "and U+2212 MINUS SIGN look like the hyphen in `open-meteo-marine:` but are not folded to "
    "'-' by NFKC, so the connector prefix is not recognised and `allowed_text` accepts them.",
)
@pytest.mark.parametrize("hyphen", ["\u2010", "\u2212"])
def test_a_lookalike_hyphen_in_a_connector_prefix_is_caught(hyphen: str) -> None:
    text = f"open{hyphen}meteo-marine:abc"
    assert kb_screen.allowed_text(text, newlines=True)
    assert kb_screen.contains_evidence_id(text)


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP, GATE FOR THE FIRST PRODUCTION LOAD (owner decision open): Latin small "
    "capitals (U+1D0B, U+0299) spell `kb` but casefold to themselves, not to 'k' and 'b', so "
    "`\u1d0b\u0299:abc` passes both screens.",
)
def test_latin_small_capitals_spelling_an_id_prefix_are_caught() -> None:
    text = "\u1d0b\u0299:abc"
    assert kb_screen.allowed_text(text, newlines=True)
    assert kb_screen.contains_evidence_id(text)


def _default_ignorable(ch: str) -> bool:
    """The Unicode DerivedCoreProperties Default_Ignorable_Code_Point set, as listed in the
    standard (stable ranges). Used only to prove the gap; not the screen."""
    code = ord(ch)
    ranges = (
        (0x00AD, 0x00AD),
        (0x034F, 0x034F),
        (0x061C, 0x061C),
        (0x115F, 0x1160),
        (0x17B4, 0x17B5),
        (0x180B, 0x180F),
        (0x200B, 0x200F),
        (0x202A, 0x202E),
        (0x2060, 0x206F),
        (0x3164, 0x3164),
        (0xFE00, 0xFE0F),
        (0xFEFF, 0xFEFF),
        (0xFFA0, 0xFFA0),
        (0xFFF0, 0xFFF8),
        (0x1BCA0, 0x1BCA3),
        (0x1D173, 0x1D17A),
        (0xE0000, 0xE0FFF),
    )
    return any(low <= code <= high for low, high in ranges)


def test_the_stable_default_ignorable_ranges_are_all_refused_today() -> None:
    """GATE for the first production load (open): the screen uses general categories and the
    Latin-letter rule, NOT a generated Default_Ignorable_Code_Point table. Checked here: every
    code point of the stable ranges listed above is refused today. The range list is written from
    the standard by hand, so this is a regression guard, not a proof of completeness; no leaking
    code point was found, so there is no failing test to mark as a known gap."""
    import sys

    leaked = [
        code
        for code in range(sys.maxunicode + 1)
        if _default_ignorable(chr(code)) and kb_screen.allowed_text(chr(code), newlines=True)
    ]
    assert leaked == []


# --- document_id_reads_as_instruction ------------------------------------------------------------

WORDS = ("ignore", "system", "assistant", "instruction", "recommend", "go", "approve")


def check_words(module: Any) -> None:
    assert set(module.ID_WORD_LIST) == set(WORDS)
    for word in WORDS:
        assert module.document_id_reads_as_instruction(word), word
        assert module.document_id_reads_as_instruction(f"{word}-notes"), word
        assert module.document_id_reads_as_instruction(f"site-{word}"), word
        assert module.document_id_reads_as_instruction(f"site-{word}-notes"), word
    for fine in ("goa-reef", "going-deep", "ignorex", "systems", "assistants", "site-guide", "a1"):
        assert not module.document_id_reads_as_instruction(fine), fine


def test_instruction_words_are_refused_as_whole_hyphen_tokens_only() -> None:
    check_words(kb_screen)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            '    return any(token in ID_WORD_LIST for token in document_id.split("-"))',
            "    return any(w in document_id for w in ID_WORD_LIST)",
        ),
        (
            '    return any(token in ID_WORD_LIST for token in document_id.split("-"))',
            "    return document_id in ID_WORD_LIST",
        ),
        (
            '    return any(token in ID_WORD_LIST for token in document_id.split("-"))',
            '    return document_id.split("-")[0] in ID_WORD_LIST',
        ),
        (
            '{"ignore", "system", "assistant", "instruction", "recommend", "go", "approve"}',
            '{"ignore", "system", "assistant", "instruction", "recommend", "go"}',
        ),
    ],
)
def test_mutation_a_wrong_word_rule_is_caught(old: str, new: str) -> None:
    mutant = mutated_module(kb_screen, (old, new))
    with pytest.raises(CAUGHT):
        check_words(mutant)
    check_words(kb_screen)


def test_the_screens_never_carry_the_text_they_screened() -> None:
    for call in (
        lambda: kb_screen.allowed_text("secret-\u200b-text", newlines=False),
        lambda: kb_screen.contains_evidence_id("secret kb:text"),
        lambda: kb_screen.document_id_reads_as_instruction("secret-go"),
    ):
        result = call()
        assert isinstance(result, bool)
