"""A wording screen for model-written `conflicting_signals` (ADR 0025 Known gaps).

Signals are LLM text shown to a human who decides on safety. The screen flags wording that reads
as an assurance, a recommendation, an instruction, a claim of system origin, an evidence-id
lookalike, mixed-script words or hidden characters. A flagged signal is KEPT and MARKED with a
fixed prefix; it is never dropped, edited or repaired. The stored text is exactly what the model
wrote (the page removes hidden characters on display) and the reader is told to read it with care.
A model that writes the prefix itself is marked again, so its text cannot pass for the code's
marker. A screen, not a control: it can miss wording, and nothing relies on it for safety. No
signal can relax the result (`reconcile` only tightens).
"""

from __future__ import annotations

import re
import unicodedata

FLAG_PREFIX = "[FLAGGED WORDING - read with care] "

_PHRASES = re.compile(
    r"\bsafe to (?:dive|go)\b|\b(?:is|are) (?:perfectly |totally |completely )?safe\b"
    r"|\bno risk\b|\brisk[- ]free\b|\bguarantee|\ball clear\b|\bconditions are perfect\b"
    r"|\bnothing to worry\b"
    r"|\bignore (?:all |the |any )?(?:previous|prior|above|rules?|instructions?|warnings?)"
    r"|\bdisregard\b"
    r"|\boverrid(?:e|ing) (?:the |all |any )?"
    r"(?:rules?|results?|recommendation|warnings?|constraints?)\b"
    r"|\bsystem prompt\b|\bdeveloper message\b"
    r"|\b(?:recommend|suggest|choose|select|set|change|upgrade|downgrade|return|output)"
    r"(?:ed|ing|s)? (?:to |as |a |the |final )?(?:go|caution|no[- ]?go)(?![-\w])"
    r"|\b(?:result|outcome|recommendation|verdict) (?:to|as|is|=) (?:go|caution|no[- ]?go)(?![-\w])"
    r"|\bproceed with the dive\b|\bdive (?:is|can go) ahead\b"
    # a claim to come from the system, e.g. "[checked by rules engine]"
    r"|\[[^\]]{0,60}(?:verified|checked|approved|validated|rules engine|supervisor)"
)
# An evidence id lookalike, e.g. "kb:12", "ev-9", "location:499". Real ids the caller passes are
# removed before this check, so only ids that do not exist are flagged. "time:0900" still is.
_ID_LOOKALIKE = re.compile(r"\b(?:[a-z_]{2,}:\w|(?:ev|kb|evidence)[-_]?\d)")
# The same key phrases with every separator removed ("s a f e", "safe_to_dive", "safe.to.dive").
_COMPACT = (
    "safetodive",
    "safetogo",
    "norisk",
    "riskfree",
    "ignoreprevious",
    "ignoretherules",
    "ignoreallprevious",
    "systemprompt",
    "conditionsareperfect",
)
# Invisible or filler characters outside the C categories.
_HIDDEN = frozenset("\u034f\u3164\uffa0\u2800\u115f\u1160\u180e") | {
    chr(c) for c in range(0xFE00, 0xFE10)
}
_MIXED = ("LATIN", "CYRILLIC", "GREEK")


def _script(ch: str) -> str:
    try:
        first = unicodedata.name(ch).split(" ", 1)[0]
    except ValueError:
        return ""
    return first if first in _MIXED else ""


def _mixed_script_word(value: str) -> bool:
    return any(len({s for ch in word if (s := _script(ch))}) > 1 for word in value.split())


def _normalised(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(text.split())


def reads_as_flagged(value: str, known_ids: frozenset[str] = frozenset()) -> bool:
    """True when the text matches a phrase, an id lookalike or a separator-free key phrase, holds
    a control, format, private-use, unassigned or hidden character, or mixes scripts in a word."""
    if any(
        ch in _HIDDEN or (unicodedata.category(ch)[0] == "C" and ch not in "\t\n") for ch in value
    ):
        return True
    if _mixed_script_word(value):
        return True
    text = _normalised(value)
    if _PHRASES.search(text):
        return True
    without_known = text
    for known in sorted((_normalised(i) for i in known_ids), key=len, reverse=True):
        without_known = without_known.replace(known, " ")
    if _ID_LOOKALIKE.search(without_known):
        return True
    compact = "".join(ch for ch in text if ch.isalnum())
    return any(phrase in compact for phrase in _COMPACT)


def mark_flagged(
    signals: tuple[str, ...], known_ids: frozenset[str] = frozenset()
) -> tuple[str, ...]:
    """Each flagged signal gets `FLAG_PREFIX`; the text is otherwise unchanged. Text that already
    starts with the prefix was written by the model, so it counts as flagged and gets the prefix
    again: the code's marker is always the outermost one."""
    return tuple(
        FLAG_PREFIX + s if s.startswith(FLAG_PREFIX) or reads_as_flagged(s, known_ids) else s
        for s in signals
    )
