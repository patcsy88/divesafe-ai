"""Text screens for the knowledge loader (ADR 0022 decisions 6, 9, 13). Pure functions; they return
booleans and never carry or echo the text they screened.

These are screens, not controls (decision 9): paraphrase and splitting across chunks get past the id
screen. Where the ADR names no table, Python's `unicodedata` general categories are used and the
gaps are listed in the loader report.
"""

from __future__ import annotations

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

# Decision 13: refused categories. Cc control, Cf format (bidi, zero-width, tag, soft hyphen),
# Co private use, Cn unassigned and noncharacters, Cs surrogates, Mn/Mc/Me combining marks (which
# include the variation selectors and the combining grapheme joiner).
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn", "Cs", "Mn", "Mc", "Me"})


def _char_allowed(ch: str, newlines: bool) -> bool:
    if ch == "\n":
        return newlines
    if ch == " ":
        return True
    category = unicodedata.category(ch)
    if category in _REFUSED_CATEGORIES:
        return False
    if category.startswith("L"):
        # Latin-script letters only (decision 13). Hangul fillers (default-ignorable, category Lo)
        # and every other non-Latin letter fail here.
        return unicodedata.name(ch, "").startswith("LATIN ")
    if category == "Nd":
        return "0" <= ch <= "9"
    # Common punctuation and symbols: all P* and the Sm, Sc, Sk symbols. So (emoji, (c)) is refused.
    return category.startswith("P") or category in {"Sm", "Sc", "Sk"}


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
