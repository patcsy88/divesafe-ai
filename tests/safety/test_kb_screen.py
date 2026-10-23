"""The loader's text screens (ADR 0022 decisions 6, 9, 13; ADR 0024 A3). These are SCREENS, not
controls (decision 9): paraphrase and splitting across chunks get past them. The known gaps are
strict xfails with their reasons, so closing one turns the test red until the marker is removed.
Mutations run on COPIES of the module source, never on `src`."""

from __future__ import annotations

import hashlib
import string
import sys
import unicodedata
from typing import Any

import pytest
from tests.safety.kb_fakes import mutated_module

from divesafe.services import kb_screen

pytestmark = pytest.mark.safety

CAUGHT = (AssertionError, Exception, pytest.fail.Exception)

# --- allowed_text: a FIXED allow-list (owner decision 2026-10-23, ADR 0022 decision 13) ----------

EXPECTED_PUNCTUATION = frozenset(".,;:!?'\"()[]-/%+=<>")
LATIN_PREFIXES = ("LATIN SMALL LETTER ", "LATIN CAPITAL LETTER ")

ACCEPTED = [
    "Plain English text.",
    "Numbers 0123456789 and punctuation , ; : ! ? ( ) - / % + = < >",
    "Open Water Diver (PADI), v2: 100% [draft]",
    "Caf\u00e9 Zo\u00eb na\u00efve \u0141\u00f3d\u017a \u00f1",  # Latin letters with diacritics
    'it\'s "ok"; yes?!',
    "CC BY-SA 4.0 / a+b=c <x>",
    "",
    " ",
]
REFUSED: dict[str, str] = {
    # typographic punctuation and spaces
    "right single quote U+2019": "it\u2019s",
    "left double quote U+201C": "\u201cok",
    "en dash U+2013": "a\u2013b",
    "em dash U+2014": "a\u2014b",
    "hyphen U+2010": "a\u2010b",
    "minus sign U+2212": "a\u2212b",
    "no-break space U+00A0": "a\u00a0b",
    "line separator": "a\u2028b",
    "ratio U+2236": "kb\u2236abc",
    "modifier letter colon U+A789": "kb\ua789abc",
    "Armenian full stop U+0589": "kb\u0589abc",
    "Greek question mark (not NFC)": "\u037e",
    # ASCII punctuation and symbols that are not on the list
    "underscore": "a_b",
    "at sign": "a@b",
    "hash": "a#b",
    "asterisk": "a*b",
    "ampersand": "a&b",
    "dollar": "a$b",
    "backslash": "a\\b",
    "left brace": "a{b",
    "right brace": "a}b",
    "caret": "a^b",
    "pipe": "a|b",
    "tilde": "a~b",
    "backtick": "a`b",
    # controls
    "NUL": "a\x00b",
    "escape": "a\x1bb",
    "tab": "a\tb",
    "carriage return": "a\rb",
    "vertical tab": "a\x0bb",
    "delete": "a\x7fb",
    "next line": "a\u0085b",
    # invisible and format characters
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
    "decomposed e acute (not NFC)": "cafe\u0301",
    # digits, symbols, private use, unassigned
    "Arabic-Indic digit": "\u0663",
    "fullwidth digit": "\uff11",
    "Devanagari digit": "\u0967",
    "emoji": "\U0001f600",
    "copyright sign": "\u00a9",
    "snowman": "\u2603",
    "private use": "\ue000",
    "unassigned": "\u0378",
    "noncharacter": "\ufffe",
    "lone surrogate": "\ud800",
    # letters whose names do not start LATIN SMALL/CAPITAL LETTER
    "feminine ordinal U+00AA": "\u00aa",
    "modifier letter U+02B0": "\u02b0",
    "Latin small capital K U+1D0B": "\u1d0b",
    "Latin small capital B U+0299": "\u0299",
    "Latin capital letter small capital I U+A7AE": "\ua7ae",
    "ligature oe": "\u0153",
    "ligature fi": "\ufb01",
    "capital ligature IJ": "\u0132",
    "Cyrillic a": "\u0430",
    "Greek alpha": "\u03b1",
    "CJK ideograph": "\u4e2d",
    "Arabic letter": "\u0627",
}


def check_allowed(module: Any) -> None:
    for text in ACCEPTED:
        assert module.allowed_text(text, newlines=False), repr(text)
    for name, text in REFUSED.items():
        assert not module.allowed_text(text, newlines=False), name
        assert not module.allowed_text(text, newlines=True), name
    assert module.ALLOWED_PUNCTUATION == EXPECTED_PUNCTUATION
    assert not module.allowed_text("a\tb", newlines=True)
    assert not module.allowed_text("a\nb", newlines=False)
    assert module.allowed_text("a\nb", newlines=True)
    for char in "_@#*&$\\{}^|~`":
        assert not module.allowed_text(char, newlines=True), char


def test_allowed_text_accepts_ordinary_text_and_refuses_each_dangerous_class() -> None:
    check_allowed(kb_screen)


def test_the_allowed_punctuation_is_exactly_these_nineteen_characters() -> None:
    assert kb_screen.ALLOWED_PUNCTUATION == EXPECTED_PUNCTUATION  # set equality, not a subset
    assert len(kb_screen.ALLOWED_PUNCTUATION) == 19
    assert all(ord(c) < 128 for c in kb_screen.ALLOWED_PUNCTUATION)


@pytest.mark.parametrize("text", ACCEPTED)
def test_each_accepted_sample_is_accepted(text: str) -> None:
    result = kb_screen.allowed_text(text, newlines=False)
    assert result is True


@pytest.mark.parametrize("name", list(REFUSED))
def test_each_refused_sample_is_refused_with_and_without_newlines(name: str) -> None:
    text = REFUSED[name]
    assert kb_screen.allowed_text(text, newlines=False) is False
    assert kb_screen.allowed_text(text, newlines=True) is False


@pytest.mark.parametrize("char", ["\u00df", "\u00e6", "\u00f8", "\u00f0", "\u00fe", "\u0131"])
def test_sharp_s_ae_o_stroke_and_similar_are_accepted_because_of_their_names(char: str) -> None:
    """name-based rule: confirm before the first production load. These letters are accepted only
    because Unicode names them LATIN SMALL LETTER ...; the ligature letters (oe, fi, IJ) are
    refused because their names say LIGATURE. If the owner wants either side to differ, the rule,
    not the letter list, has to change."""
    assert unicodedata.name(char).startswith(LATIN_PREFIXES)
    assert kb_screen.allowed_text(char, newlines=False) is True


def test_the_whole_ascii_range_is_exactly_letters_digits_space_the_list_and_newline() -> None:
    allowed = {
        chr(code) for code in range(128) if kb_screen.allowed_text(chr(code), newlines=False)
    }
    expected = set(string.ascii_letters) | set(string.digits) | {" "} | set(EXPECTED_PUNCTUATION)
    assert allowed == expected
    assert kb_screen.allowed_text("\n", newlines=True)
    assert not kb_screen.allowed_text("\n", newlines=False)


def test_every_non_ascii_character_accepted_is_a_latin_named_letter() -> None:
    """Sweeps the whole code space: nothing but L* letters named LATIN SMALL/CAPITAL LETTER."""
    leaked = []
    for code in range(128, sys.maxunicode + 1):
        char = chr(code)
        if not kb_screen.allowed_text(char, newlines=True):
            continue
        letter = unicodedata.category(char).startswith("L")
        named = unicodedata.name(char, "").startswith(LATIN_PREFIXES)
        if not (letter and named):
            leaked.append(code)
    assert leaked == []


def test_a_decomposed_accent_is_refused_but_its_composed_form_is_accepted() -> None:
    assert kb_screen.allowed_text("\u00e9", newlines=False)
    assert not kb_screen.allowed_text("e\u0301", newlines=False)


def test_a_newline_is_allowed_only_when_asked_for() -> None:
    assert kb_screen.allowed_text("a\nb", newlines=True)
    assert not kb_screen.allowed_text("a\nb", newlines=False)
    assert not kb_screen.allowed_text("a\r\nb", newlines=True)


def test_every_refused_example_is_non_empty_and_the_table_names_are_unique() -> None:
    """Guards the table itself: a typo in an example would make a test vacuous."""
    for name, text in REFUSED.items():
        assert text, name
    assert unicodedata.category("\u200b") == "Cf"
    assert unicodedata.category("\ufe0f") == "Mn"
    assert unicodedata.category("\u3164") == "Lo"
    assert unicodedata.category("\u0663") == "Nd"
    assert unicodedata.category("\U0001f600") == "So"
    assert unicodedata.name("\u1d0b").startswith("LATIN LETTER ")
    assert unicodedata.name("\u0299").startswith("LATIN LETTER ")


ASCII_RULE = (
    "    if ord(ch) < 128:  # any other ASCII character, controls included\n        return False"
)
NON_ASCII_CATEGORY = '    if not unicodedata.category(ch).startswith("L"):'
NAME_RULE = (
    "    return name.startswith(_LATIN_NAME_PREFIXES) and not any(\n"
    "        fragment in name for fragment in REFUSED_NAME_FRAGMENTS\n"
    "    )"
)
PREFIX_ONLY = "    return name.startswith(_LATIN_NAME_PREFIXES)"
FRAGMENTS_LINE = (
    'REFUSED_NAME_FRAGMENTS: tuple[str, ...] = ("SMALL CAPITAL", "LIGATURE", "MODIFIER")'
)
SPACE_RULE = '    if ch == " " or ch in _ASCII_LETTERS_AND_DIGITS or ch in ALLOWED_PUNCTUATION:'

ALLOW_LIST_MUTATIONS = [
    pytest.param(
        ('frozenset(".,;:!?', 'frozenset("@.,;:!?'), "a@b", id="punctuation-widened-by-one"
    ),
    pytest.param(('frozenset(".,;:!?', 'frozenset("_.,;:!?'), "a_b", id="underscore-added"),
    pytest.param(
        (ASCII_RULE, '    if ord(ch) < 128:\n        return unicodedata.category(ch)[0] in "PS"'),
        "a@b",
        id="any-ascii-P-or-S",
    ),
    pytest.param(
        (
            NON_ASCII_CATEGORY,
            '    if unicodedata.category(ch)[0] in "PS":\n        return True\n'
            + NON_ASCII_CATEGORY,
        ),
        "a\u2019b",
        id="any-non-ascii-P-or-S",
    ),
    pytest.param((NAME_RULE, "    return True"), "\u1d0b", id="any-L-letter"),
    pytest.param(
        (NAME_RULE, '    return name.startswith("LATIN ")'),
        "\u1d0b",
        id="prefix-loosened-to-LATIN",
    ),
    pytest.param((NAME_RULE, PREFIX_ONLY), "\ua7ae", id="fragment-check-dropped"),
    pytest.param(
        (FRAGMENTS_LINE, FRAGMENTS_LINE.replace('"SMALL CAPITAL", ', "")),
        "\ua7ae",
        id="small-capital-fragment-removed",
    ),
    pytest.param((SPACE_RULE, SPACE_RULE.replace('ch == " "', 'ch in " \\t"')), "a\tb", id="tab"),
    pytest.param(
        (SPACE_RULE, SPACE_RULE.replace('ch == " "', "ch.isspace()")),
        "a\u00a0b",
        id="any-space",
    ),
    pytest.param(
        (
            '    if ch == "\\n":\n        return newlines',
            '    if ch == "\\n":\n        return True',
        ),
        "a\nb",
        id="newline-always-allowed",
    ),
    pytest.param(
        (
            '    if ch == "\\n":\n        return newlines',
            '    if ch == "\\n":\n        return False',
        ),
        "a\nb",
        id="newline-never-allowed",
    ),
]


@pytest.mark.parametrize(("edit", "probe"), ALLOW_LIST_MUTATIONS)
def test_mutation_a_weaker_allow_list_is_caught(edit: tuple[str, str], probe: str) -> None:
    mutant = mutated_module(kb_screen, edit)
    newlines = probe == "a\nb"
    real = kb_screen.allowed_text(probe, newlines=newlines)
    mutated = mutant.allowed_text(probe, newlines=newlines)
    if edit[1].endswith("return False") and probe == "a\nb":
        assert real and not mutated  # the opposite direction: a stricter mutant loses text
    elif probe == "a\nb":
        assert not kb_screen.allowed_text(probe, newlines=False)
        assert mutant.allowed_text(probe, newlines=False)
    else:
        assert not real
        assert mutated
    with pytest.raises(CAUGHT):
        check_allowed(mutant)
    check_allowed(kb_screen)


def test_the_name_rule_has_teeth_against_every_latin_named_exception() -> None:
    """Dropping the name rule must let in every non-Latin letter the REFUSED table lists."""
    mutant = mutated_module(kb_screen, (NAME_RULE, "    return True"))
    for name in ("Cyrillic a", "Greek alpha", "CJK ideograph", "Arabic letter", "ligature fi"):
        assert mutant.allowed_text(REFUSED[name], newlines=False), name
        assert not kb_screen.allowed_text(REFUSED[name], newlines=False), name


def test_every_accepted_character_is_stable_under_nfc() -> None:
    """Why the NFC check in `allowed_text` is now an equivalent mutant (not tested): a decomposed
    sequence needs a combining mark, and no mark is on the list; every accepted character is its
    own NFC form."""
    unstable = [
        code
        for code in range(sys.maxunicode + 1)
        if kb_screen.allowed_text(chr(code), newlines=True)
        and unicodedata.normalize("NFC", chr(code)) != chr(code)
    ]
    assert unstable == []


# Equivalent mutants, deliberately NOT tested (each is refused by the other wall anyway):
#   - dropping the NFC check in `allowed_text` (see the test above);
#   - dropping `"\r" in value`: a carriage return is ASCII and not on the list;
#   - dropping the first NFKC/casefold line of `_fold`: the same fold is applied again at the end.


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


LOOKALIKES = [
    pytest.param("kb\u2236abc", id="ratio-as-colon"),
    pytest.param("kb\ua789abc", id="modifier-colon"),
    pytest.param("kb\u0589abc", id="armenian-full-stop-as-colon"),
    pytest.param("open\u2010meteo-marine:abc", id="hyphen-U+2010"),
    pytest.param("open\u2212meteo-marine:abc", id="minus-U+2212"),
    pytest.param("\u1d0b\u0299:abc", id="small-capitals-kb"),
]


@pytest.mark.parametrize("text", LOOKALIKES)
def test_a_lookalike_of_an_id_marker_is_refused_by_the_allow_list(text: str) -> None:
    """The control is the allow-list (owner decision 2026-10-23): none of these characters is on
    it, so a chunk or a manifest text field holding one is refused."""
    assert kb_screen.allowed_text(text, newlines=True) is False
    assert kb_screen.allowed_text(text, newlines=False) is False


@pytest.mark.parametrize("text", LOOKALIKES)
def test_the_id_screen_alone_still_does_not_see_the_lookalikes(text: str) -> None:
    """Documents that the allow-list, not `contains_evidence_id`, is what stops these. If the id
    screen ever learns to fold them this test goes red: then delete it."""
    assert kb_screen.contains_evidence_id(text) is False


@pytest.mark.parametrize("text", LOOKALIKES)
def test_mutation_without_the_allow_list_the_lookalikes_get_through_both_screens(
    text: str,
) -> None:
    mutant = mutated_module(kb_screen, (NAME_RULE, "    return True"))
    loosened = mutated_module(
        kb_screen,
        (NAME_RULE, "    return True"),
        (
            NON_ASCII_CATEGORY,
            '    if unicodedata.category(ch)[0] in "PS":\n        return True\n'
            + NON_ASCII_CATEGORY,
        ),
    )
    assert kb_screen.allowed_text(text, newlines=True) is False
    survived = mutant.allowed_text(text, newlines=True) or loosened.allowed_text(
        text, newlines=True
    )
    assert survived  # at least one weaker allow-list lets the look-alike in


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


# --- name fragments, the pinned accepted set, the recorded gap (round 10) -------------------------

# U+A7AE is named "LATIN CAPITAL LETTER SMALL CAPITAL I": it passes the prefix rule and is refused
# only by the SMALL CAPITAL fragment.
PINNED_UNIDATA_VERSION = "16.0.0"
PINNED_ACCEPTED_NON_ASCII_COUNT = 1060
PINNED_ACCEPTED_NON_ASCII_SHA256 = (
    "010f0302438ded0e0be97964b61c1328eb14b5a2bb3b616f092eb997b43aa8b2"
)


def accepted_non_ascii(module: Any) -> list[int]:
    return [
        code
        for code in range(128, sys.maxunicode + 1)
        if module.allowed_text(chr(code), newlines=True)
    ]


def fingerprint(module: Any) -> tuple[int, str]:
    codes = accepted_non_ascii(module)
    return len(codes), hashlib.sha256(",".join(map(str, codes)).encode()).hexdigest()


def check_pinned_set(module: Any) -> None:
    assert unicodedata.unidata_version == PINNED_UNIDATA_VERSION, (
        f"Unicode data is {unicodedata.unidata_version}, the accepted set was reviewed under "
        f"{PINNED_UNIDATA_VERSION}: the set of accepted non-ASCII characters MUST be re-reviewed "
        "(ADR review) and the pinned constants updated; do not skip this test"
    )
    count, digest = fingerprint(module)
    assert (count, digest) == (PINNED_ACCEPTED_NON_ASCII_COUNT, PINNED_ACCEPTED_NON_ASCII_SHA256), (
        "the set of accepted non-ASCII characters changed; widening it needs an ADR review"
    )


def test_the_accepted_non_ascii_set_is_pinned_by_version_count_and_hash() -> None:
    """Widening the accepted set needs ADR review. A different Unicode version fails loudly: the
    set must be re-reviewed, then the constants updated."""
    check_pinned_set(kb_screen)


def test_mutation_widening_the_accepted_set_by_one_code_point_fails_the_pin() -> None:
    widened = mutated_module(
        kb_screen, (NAME_RULE, NAME_RULE.replace("return ", "return ch == '\\u00b5' or "))
    )
    assert widened.allowed_text("\u00b5", newlines=False)
    assert not kb_screen.allowed_text("\u00b5", newlines=False)
    with pytest.raises(CAUGHT):
        check_pinned_set(widened)
    check_pinned_set(kb_screen)


def test_mutation_accepting_u_a7ae_fails_the_pin_too() -> None:
    mutant = mutated_module(kb_screen, (NAME_RULE, PREFIX_ONLY))
    with pytest.raises(CAUGHT):
        check_pinned_set(mutant)


def test_the_refused_name_fragments_are_exactly_these_three() -> None:
    assert kb_screen.REFUSED_NAME_FRAGMENTS == ("SMALL CAPITAL", "LIGATURE", "MODIFIER")


@pytest.mark.parametrize("newlines", [False, True])
def test_u_a7ae_is_refused_with_and_without_newlines(newlines: bool) -> None:
    char = "\ua7ae"
    assert unicodedata.name(char) == "LATIN CAPITAL LETTER SMALL CAPITAL I"
    assert unicodedata.name(char).startswith(LATIN_PREFIXES)  # only the fragment refuses it
    assert kb_screen.allowed_text(char, newlines=newlines) is False
    assert kb_screen.allowed_text(f"a{char}b\n", newlines=newlines) is False


def test_no_accepted_non_ascii_character_has_a_refused_fragment_in_its_name() -> None:
    bad = [
        code
        for code in accepted_non_ascii(kb_screen)
        if any(f in unicodedata.name(chr(code), "") for f in kb_screen.REFUSED_NAME_FRAGMENTS)
    ]
    assert bad == []


def test_small_capital_is_the_only_fragment_reachable_under_the_prefix_rule() -> None:
    """EQUIVALENT MUTANTS, honestly: under Unicode 16.0.0 no letter named LATIN SMALL/CAPITAL
    LETTER ... holds LIGATURE or MODIFIER, so removing either from the tuple changes nothing and is
    deliberately not mutation-tested. Dropping the whole fragment check is caught via U+A7AE only.
    If a future Unicode version makes another fragment reachable, this test goes red: add its
    mutation then."""
    reachable = {
        fragment: [
            code
            for code in range(128, sys.maxunicode + 1)
            if unicodedata.category(chr(code)).startswith("L")
            and unicodedata.name(chr(code), "").startswith(LATIN_PREFIXES)
            and fragment in unicodedata.name(chr(code))
        ]
        for fragment in kb_screen.REFUSED_NAME_FRAGMENTS
    }
    assert reachable == {"SMALL CAPITAL": [0xA7AE], "LIGATURE": [], "MODIFIER": []}


def test_mutation_removing_a_fragment_individually() -> None:
    mutant = mutated_module(
        kb_screen, (FRAGMENTS_LINE, FRAGMENTS_LINE.replace('"SMALL CAPITAL", ', ""))
    )
    assert mutant.allowed_text("\ua7ae", newlines=False)
    with pytest.raises(CAUGHT):
        check_allowed(mutant)
    for fragment in ("LIGATURE", "MODIFIER"):  # equivalent mutants: nothing reachable changes
        edit = (FRAGMENTS_LINE, FRAGMENTS_LINE.replace(f', "{fragment}"', ""))
        same = mutated_module(kb_screen, edit)
        assert accepted_non_ascii(same) == accepted_non_ascii(kb_screen), fragment


NAME_FACTS = [
    # char, name, accepted
    ("\u00df", "LATIN SMALL LETTER SHARP S", True),
    ("\u00e6", "LATIN SMALL LETTER AE", True),
    ("\u00f8", "LATIN SMALL LETTER O WITH STROKE", True),
    ("\u00f0", "LATIN SMALL LETTER ETH", True),
    ("\u00fe", "LATIN SMALL LETTER THORN", True),
    ("\u0131", "LATIN SMALL LETTER DOTLESS I", True),
    ("\u0153", "LATIN SMALL LIGATURE OE", False),  # NOT "LETTER OE": the ligature is refused
]


@pytest.mark.parametrize(("char", "name", "accepted"), NAME_FACTS)
def test_the_name_and_the_behaviour_of_each_special_latin_letter(
    char: str, name: str, accepted: bool
) -> None:
    assert unicodedata.name(char) == name
    assert kb_screen.allowed_text(char, newlines=False) is accepted


# RECORDED GAP, owner-accepted 2026-10-23: Latin-named lookalike letters pass both screens (no
# wildcard or confusables matching). If the screens ever catch them, revisit these tests.
@pytest.mark.parametrize(
    "char", ["\u0138", "\u0185", "\u0131", "\u0269", "\u0251", "\u0261", "\u1e35", "\u0137"]
)
def test_recorded_gap_latin_named_lookalike_letters_pass_both_screens(char: str) -> None:
    assert unicodedata.name(char).startswith(LATIN_PREFIXES)
    assert kb_screen.allowed_text(char, newlines=True) is True
    assert kb_screen.contains_evidence_id(f"{char}b:abc") is False


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP (decision 9, screens not controls): `_fold` removes only separators, so "
    "dropping, spacing with punctuation, or percent-encoding the id marker is not recognised.",
)
@pytest.mark.parametrize(
    "text", ["openmeteomarine:x", "open meteo marine:x", "open.meteo.marine:x", "kb%3Aabc"]
)
def test_a_connector_prefix_with_hyphens_dropped_or_a_percent_encoded_id_is_caught(
    text: str,
) -> None:
    assert kb_screen.contains_evidence_id(text)
