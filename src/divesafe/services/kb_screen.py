"""Text screens for the knowledge loader (ADR 0022 decisions 6, 9, 13). Pure functions; they return
booleans and never carry or echo the text they screened.

These are screens, not controls (decision 9): paraphrase and splitting across chunks get past the id
screen. Where the ADR names no table, Python's `unicodedata` general categories are used and the
gaps are listed in the loader report.
"""

from __future__ import annotations

import string
import unicodedata

from divesafe.data import (
    data_gov_my_forecast,
    data_gov_my_warnings,
    met_api_marine,
    open_meteo_marine,
    open_meteo_wind,
    phn_etide,
)

# Decision 9: "the prefixes are listed in code from the connectors, not a generic pattern". Each
# connector's own SOURCE constant is the prefix of its evidence ids ("<SOURCE>:...").
KB_ID_PREFIX = "kb"
CONNECTOR_PREFIXES: tuple[str, ...] = (
    data_gov_my_forecast.SOURCE,
    data_gov_my_warnings.SOURCE,
    met_api_marine.SOURCE,
    open_meteo_marine.SOURCE,
    open_meteo_wind.SOURCE,
    phn_etide.SOURCE,
)

# Decision 6: tokens named by the ADR, "and similar" (the ADR fixes no fuller list). Whole token.
ID_WORD_LIST = frozenset(
    {"ignore", "system", "assistant", "instruction", "recommend", "go", "approve"}
)

# Decision 13 (owner decision 2026-10-23): a FIXED allow-list. Letters and digits are ASCII, plus
# accented Latin letters by Unicode name; the only punctuation and symbols are these ASCII
# characters. Every other character is refused, including all non-ASCII P* and S* characters
# (curly quotes, dashes, U+2212 and the like), "LATIN LETTER ..." names (small capitals) and
# modifier letters. Pin this constant in tests.
ALLOWED_PUNCTUATION = frozenset(".,;:!?'\"()[]-/%+=<>")

_ASCII_LETTERS_AND_DIGITS = frozenset(string.ascii_letters + string.digits)
_LATIN_NAME_PREFIXES = ("LATIN SMALL LETTER ", "LATIN CAPITAL LETTER ")
# A Latin-named letter whose name holds one of these is a lookalike form, not an accented letter
# (e.g. U+A7AE "LATIN CAPITAL LETTER SMALL CAPITAL I"). Other Latin-named lookalikes are a
# recorded gap (owner decision): no wildcard matching and no confusables map.
REFUSED_NAME_FRAGMENTS: tuple[str, ...] = ("SMALL CAPITAL", "LIGATURE", "MODIFIER")


def _char_allowed(ch: str, newlines: bool) -> bool:
    if ch == "\n":
        return newlines
    if ch == " " or ch in _ASCII_LETTERS_AND_DIGITS or ch in ALLOWED_PUNCTUATION:
        return True
    if ord(ch) < 128:  # any other ASCII character, controls included
        return False
    if not unicodedata.category(ch).startswith("L"):
        return False  # marks, format, private use, unassigned, digits, P*, S*, Z* and the rest
    name = unicodedata.name(ch, "")
    return name.startswith(_LATIN_NAME_PREFIXES) and not any(
        fragment in name for fragment in REFUSED_NAME_FRAGMENTS
    )


def allowed_text(value: str, *, newlines: bool) -> bool:
    """NFC already, no carriage return, every character on the allow-list."""
    if unicodedata.normalize("NFC", value) != value or "\r" in value:
        return False
    return all(_char_allowed(ch, newlines) for ch in value)


# \n, \r, \t and the other whitespace controls (Cc, so not Z*) must not split a marker either.
_WHITESPACE = frozenset("\t\n\r\x0b\x0c\x1c\x1d\x1e\x1f\x85")


def _fold(value: str) -> str:
    """NFKC, case folding, then removal of separators (Unicode category Z*; the ADR does not
    define "separators", see the report). Folded again after removal so a join cannot hide an id."""
    text = unicodedata.normalize("NFKC", value).casefold()
    text = "".join(
        ch for ch in text if not (unicodedata.category(ch).startswith("Z") or ch in _WHITESPACE)
    )
    return unicodedata.normalize("NFKC", text).casefold()


def contains_evidence_id(value: str) -> bool:
    """True when the chunk holds a `kb:` id or an id with a real connector prefix (decision 9)."""
    folded = _fold(value)
    markers = tuple(f"{p}:" for p in (KB_ID_PREFIX, *CONNECTOR_PREFIXES))
    return any(marker in folded for marker in markers)


def document_id_reads_as_instruction(document_id: str) -> bool:
    """True when a hyphen-separated token of the id is on the word list (decision 6)."""
    return any(token in ID_WORD_LIST for token in document_id.split("-"))
