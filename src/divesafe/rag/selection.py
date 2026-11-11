"""Version selection and classification for the knowledge store, in code (ADR 0022 decision 3).

The same selection runs in SQL (`rag.store`); this module repeats it from the per-version
summary the store returns, so the connector can check that the database chose the same versions,
and it alone classifies each document family into the decision 3 table rows and the decision 14
issues and notes. Pure: no I/O, no clock. Dates are Asia/Kuala_Lumpur calendar dates taken with
the fixed offset +08:00 (Malaysia has no daylight saving time; `zoneinfo` would need tz data).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum

from divesafe.domain import KnowledgeNote, KnowledgeNoteCode

KL = timezone(timedelta(hours=8))

APPROVED = "APPROVED"
WITHDRAWN = "WITHDRAWN"
DRAFT = "DRAFT"
STATUSES = frozenset({DRAFT, APPROVED, WITHDRAWN})

# Fixed issue wording (decision 3 and 14). No title and no document id, ever.
CLOCK_ISSUE = (
    "knowledge store/run clock disagreement: an approved document family is dated after this "
    "run's clock"
)
UNCHECKED_ISSUE = "knowledge store: an approved document family has an unchecked approval"
# Builder choices (reviewed by risk-reviewer, ADR 0022): wording the ADR does not fix.
INCONSISTENT_ISSUE = "knowledge store: a document family has an inconsistent status history"
ALL_EXPIRED_ISSUE = (
    "knowledge store: every document family that applies to this site is past its review-by date"
)


def local_date(instant: datetime) -> date:
    """The Asia/Kuala_Lumpur calendar date of an aware instant."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("an aware datetime is required")
    return instant.astimezone(KL).date()


def local_midnight(day: date) -> datetime:
    """00:00 Asia/Kuala_Lumpur on `day`, as an aware datetime at +08:00."""
    return datetime.combine(day, time(0), tzinfo=KL)


def valid_from(effective_date: date) -> datetime:
    """A document is in force from 00:00 local on its effective date."""
    return local_midnight(effective_date)


def valid_until(review_by_date: date) -> datetime:
    """00:00 local on the day after review-by: exclusive, so review-by itself is usable."""
    return local_midnight(review_by_date + timedelta(days=1))


def usable_for(review_by_date: date, window_end: datetime) -> bool:
    """The strict comparison SQL uses too: the window END is before `valid_until`."""
    return window_end < valid_until(review_by_date)


@dataclass(frozen=True)
class VersionRow:
    """One document version as the store summarises it: dates, status facts, chunk count.
    Never a title. `approved_on` is the local date of the APPROVED event; `hash_checked` is True
    only when every APPROVED event of the version is checked; `withdrawn` is True when any
    WITHDRAWN event exists; `latest` is the status of the last event by server order."""

    document_id: str
    version: int
    site_id: str | None
    all_sites: bool
    effective_date: date
    review_by_date: date
    has_events: bool
    approved: bool
    hash_checked: bool
    withdrawn: bool
    approved_on: date | None
    latest: str | None
    chunks: int

    def applies_to(self, site_id: str) -> bool:
        """The site filter: this site's own document, or an all-sites document."""
        return self.all_sites or self.site_id == site_id

    def __post_init__(self) -> None:
        if self.all_sites != (self.site_id is None):
            raise ValueError("a version is either for one site or for all sites")
        if self.effective_date > self.review_by_date:
            raise ValueError("review-by is before the effective date")
        if self.latest is not None and self.latest not in STATUSES:
            raise ValueError("unknown status")
        if self.has_events != (self.latest is not None):
            raise ValueError("a version with events has a latest status")
        if self.approved != (self.approved_on is not None):
            raise ValueError("an approved version has an approval date")
        if self.approved and not self.has_events:
            raise ValueError("an approved version has events")
        if self.chunks < 0:
            raise ValueError("chunk count is negative")


class FamilyClass(StrEnum):
    RETURNED = "returned"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"
    NOT_IN_FORCE = "not_in_force"
    CLOCK_ISSUE = "clock_issue"
    UNCHECKED_ISSUE = "unchecked_issue"
    INCONSISTENT_ISSUE = "inconsistent_issue"


ISSUE_CLASSES: dict[FamilyClass, str] = {
    FamilyClass.CLOCK_ISSUE: CLOCK_ISSUE,
    FamilyClass.UNCHECKED_ISSUE: UNCHECKED_ISSUE,
    FamilyClass.INCONSISTENT_ISSUE: INCONSISTENT_ISSUE,
}


@dataclass(frozen=True)
class FamilyResult:
    """How one family was classified. `version` and `chunks` name the governing version when
    the family is returned; otherwise None and 0."""

    document_id: str
    kind: FamilyClass
    version: int | None = None
    chunks: int = 0


def _clean(row: VersionRow, today: date) -> bool:
    """A version that may be used and that clears an issue above it: approved and checked,
    current status APPROVED with no WITHDRAWN event, approval dated on or before the run's local
    date. Being in force is checked by the caller."""
    return (
        row.approved
        and row.hash_checked
        and not row.withdrawn
        and row.latest == APPROVED
        and row.approved_on is not None
        and row.approved_on <= today
    )


def classify_family(
    rows: Sequence[VersionRow], now: datetime, window_end: datetime
) -> FamilyResult:
    """One family, by the decision 3 table with the issue rows first.

    The governing version is the highest version that was ever approved and is in force by the
    window end. Walk down from it to the first clean version: an issue is raised when any version
    above that one is dated after `now` (the clock issue, which wins) or has an unchecked approval
    (owner decision 2026-11-09: the prose of decision 3 wins over the table). Nothing ever falls
    back to a lower version: only a clean governing version returns its chunks."""
    if not rows or len({r.document_id for r in rows}) != 1:
        raise ValueError("one family, at least one version")
    family = rows[0].document_id
    today = local_date(now)
    candidates = sorted(
        (r for r in rows if r.approved and valid_from(r.effective_date) <= window_end),
        key=lambda r: r.version,
        reverse=True,
    )
    if not candidates:
        return FamilyResult(family, FamilyClass.NOT_IN_FORCE)
    governing = candidates[0]
    above: list[VersionRow] = []
    for row in candidates:
        if _clean(row, today):
            break
        above.append(row)
    if any(r.approved_on is None or r.approved_on > today for r in above):
        return FamilyResult(family, FamilyClass.CLOCK_ISSUE)
    if any(not r.hash_checked for r in above):
        return FamilyResult(family, FamilyClass.UNCHECKED_ISSUE)
    if not above:  # the governing version is clean
        if usable_for(governing.review_by_date, window_end):
            return FamilyResult(family, FamilyClass.RETURNED, governing.version, governing.chunks)
        return FamilyResult(family, FamilyClass.EXPIRED)
    if governing.withdrawn:
        return FamilyResult(family, FamilyClass.WITHDRAWN)
    return FamilyResult(family, FamilyClass.INCONSISTENT_ISSUE)


def classify(
    rows: Iterable[VersionRow], now: datetime, window_end: datetime
) -> tuple[FamilyResult, ...]:
    """Every family the rows name, in document id order. Versions with no status event are
    ignored by selection; a family made only of them counts as not in force."""
    families: dict[str, list[VersionRow]] = {}
    for row in rows:
        families.setdefault(row.document_id, []).append(row)
    return tuple(classify_family(families[name], now, window_end) for name in sorted(families))


def family_issues(results: Sequence[FamilyResult]) -> tuple[str, ...]:
    """One issue per issue family, in family order, then the all-expired issue: every family
    that would otherwise be returned (its governing version clean, withdrawn excluded) is past
    review-by."""
    issues = [ISSUE_CLASSES[r.kind] for r in results if r.kind in ISSUE_CLASSES]
    kinds = [r.kind for r in results]
    if FamilyClass.EXPIRED in kinds and FamilyClass.RETURNED not in kinds:
        issues.append(ALL_EXPIRED_ISSUE)
    return tuple(issues)


def family_notes(results: Sequence[FamilyResult]) -> tuple[KnowledgeNote, ...]:
    """Notes for the families that are not issues, in the domain's one note enum (wiring
    condition 1). No family at all is "none returned" with count 0. Expired is a note only beside
    a returned family (otherwise it is the all-expired issue)."""
    if not results:
        return (KnowledgeNote(code=KnowledgeNoteCode.NONE_RETURNED),)
    counts = {kind: sum(1 for r in results if r.kind is kind) for kind in FamilyClass}
    notes: list[KnowledgeNote] = []
    for kind, code in (
        (FamilyClass.NOT_IN_FORCE, KnowledgeNoteCode.NOT_IN_FORCE),
        (FamilyClass.WITHDRAWN, KnowledgeNoteCode.WITHDRAWN),
    ):
        if counts[kind]:
            notes.append(KnowledgeNote(code=code, count=counts[kind]))
    if counts[FamilyClass.EXPIRED] and counts[FamilyClass.RETURNED]:
        notes.append(
            KnowledgeNote(code=KnowledgeNoteCode.EXPIRED, count=counts[FamilyClass.EXPIRED])
        )
    return tuple(notes)
