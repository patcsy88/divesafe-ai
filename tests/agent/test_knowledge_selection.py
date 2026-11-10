"""Decision 3 of ADR 0022 in code: every table row, the issue-row precedence, the owner's walk-down
reading (2026-11-09: the prose wins over the table) and the dates. Each guard has a mutation
check that edits a copy of `rag.selection` and must flip a named case."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest
from tests.safety.kb_fakes import mutated_module

from divesafe.rag import selection
from divesafe.rag.selection import (
    ALL_EXPIRED_ISSUE,
    CLOCK_ISSUE,
    INCONSISTENT_ISSUE,
    UNCHECKED_ISSUE,
    FamilyClass,
    FamilyResult,
    Note,
    NoteCode,
    VersionRow,
)

pytestmark = pytest.mark.safety

KL = timezone(timedelta(hours=8))
NOW = datetime(2026, 6, 15, 10, 0, tzinfo=KL)
TODAY = date(2026, 6, 15)
END = NOW + timedelta(hours=3)
DAY = timedelta(days=1)
F = FamilyClass


def v(
    version: int,
    *,
    doc: str = "doc-a",
    eff: date = TODAY - 10 * DAY,
    rev: date = TODAY + 90 * DAY,
    approved_on: date | None = TODAY - 5 * DAY,
    checked: bool = True,
    withdrawn: bool = False,
    latest: str | None = "",
    chunks: int = 2,
    site: str | None = None,
) -> VersionRow:
    if latest == "":
        latest = "WITHDRAWN" if withdrawn else ("APPROVED" if approved_on else "DRAFT")
    return VersionRow(
        document_id=doc,
        version=version,
        site_id=site,
        all_sites=site is None,
        effective_date=eff,
        review_by_date=rev,
        has_events=latest is not None,
        approved=approved_on is not None,
        hash_checked=checked if approved_on is not None else False,
        withdrawn=withdrawn,
        approved_on=approved_on,
        latest=latest,
        chunks=chunks,
    )


TOMORROW = TODAY + DAY
OLD = {"rev": TODAY - DAY, "eff": TODAY - 2 * DAY}
# name -> (versions, expected class, expected returned version)
CASES: dict[str, tuple[list[VersionRow], FamilyClass, int | None]] = {
    "clean-only": ([v(1)], F.RETURNED, 1),
    "clock-over-clean": ([v(1), v(2, approved_on=TOMORROW)], F.CLOCK_ISSUE, None),
    "clock-withdrawn": ([v(1), v(2, approved_on=TOMORROW, withdrawn=True)], F.CLOCK_ISSUE, None),
    "clock-expired": ([v(1), v(2, approved_on=TOMORROW, **OLD)], F.CLOCK_ISSUE, None),
    "unchecked-over-clean": ([v(1), v(2, checked=False)], F.UNCHECKED_ISSUE, None),
    "unchecked-withdrawn": (
        [v(1), v(2, checked=False, withdrawn=True)],
        F.UNCHECKED_ISSUE,
        None,
    ),
    "unchecked-expired": ([v(1), v(2, checked=False, **OLD)], F.UNCHECKED_ISSUE, None),
    "unchecked-only": ([v(1, checked=False)], F.UNCHECKED_ISSUE, None),
    "clock-and-unchecked-one-issue": (
        [v(1), v(2, checked=False, approved_on=TOMORROW)],
        F.CLOCK_ISSUE,
        None,
    ),
    "unchecked-cleared-by-newer-clean": ([v(1), v(2, checked=False), v(3)], F.RETURNED, 3),
    "unchecked-not-cleared-by-withdrawn": (
        [v(1), v(2, checked=False), v(3, withdrawn=True)],
        F.UNCHECKED_ISSUE,
        None,
    ),
    "unchecked-not-cleared-by-future-dated": (
        [v(1), v(2, checked=False), v(3, approved_on=TOMORROW)],
        F.CLOCK_ISSUE,
        None,
    ),
    # Owner decision 2026-11-09 (B2): a withdrawn version over an unchecked one is the issue.
    "withdrawn-over-unchecked": (
        [v(1, checked=False), v(2, withdrawn=True)],
        F.UNCHECKED_ISSUE,
        None,
    ),
    "withdrawn-over-unchecked-over-clean": (
        [v(1), v(2, checked=False), v(3, withdrawn=True)],
        F.UNCHECKED_ISSUE,
        None,
    ),
    "withdrawn-over-clean": ([v(1), v(2, withdrawn=True)], F.WITHDRAWN, None),
    "withdrawn-expired-over-clean": ([v(1), v(2, withdrawn=True, **OLD)], F.WITHDRAWN, None),
    "withdrawn-only": ([v(1, withdrawn=True)], F.WITHDRAWN, None),
    # A WITHDRAWN event ordered below a later APPROVED one is still final.
    "withdrawn-event-below-latest-approved": (
        [v(1, withdrawn=True, latest="APPROVED")],
        F.WITHDRAWN,
        None,
    ),
    "expired": ([v(1, rev=TODAY - DAY, eff=TODAY - 30 * DAY)], F.EXPIRED, None),
    "expired-governing-hides-lower": ([v(1), v(2, **OLD)], F.EXPIRED, None),
    "future-effective-over-clean": ([v(1), v(2, eff=TODAY + 5 * DAY)], F.RETURNED, 1),
    "future-effective-withdrawn-over-clean": (
        [v(1), v(2, eff=TODAY + 5 * DAY, withdrawn=True)],
        F.RETURNED,
        1,
    ),
    "takes-effect-in-window": ([v(1), v(2, eff=TODAY)], F.RETURNED, 2),
    "draft-over-clean": ([v(1), v(2, approved_on=None)], F.RETURNED, 1),
    "draft-withdrawn-over-clean": (
        [v(1), v(2, approved_on=None, withdrawn=True)],
        F.RETURNED,
        1,
    ),
    "draft-only": ([v(1, approved_on=None)], F.NOT_IN_FORCE, None),
    "no-events": ([v(1, approved_on=None, latest=None)], F.NOT_IN_FORCE, None),
    "future-effective-only": ([v(1, eff=TODAY + 5 * DAY)], F.NOT_IN_FORCE, None),
    "approved-but-latest-draft": ([v(1, latest="DRAFT")], F.INCONSISTENT_ISSUE, None),
    "review-by-today": ([v(1, rev=TODAY, eff=TODAY - 30 * DAY)], F.RETURNED, 1),
    # Risk review n2: a future-dated version BELOW a clean governing one is not judged.
    "clock-below-clean": ([v(1), v(2, approved_on=TOMORROW), v(3)], F.RETURNED, 3),
}


def _run(module: Any, name: str) -> Any:
    return module.classify_family(CASES[name][0], NOW, END)


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_decision_3_row(name: str) -> None:
    rows, kind, version = CASES[name]
    result = selection.classify_family(rows, NOW, END)
    assert result.kind is kind
    assert result.version == version
    if version is not None:
        assert result.chunks == next(r.chunks for r in rows if r.version == version)
    else:
        assert result.chunks == 0


def test_a_window_reaching_the_exclusive_end_is_expired() -> None:
    rows = [v(1, rev=TODAY, eff=TODAY - 30 * DAY)]
    last_second = datetime(2026, 6, 15, 23, 59, 59, tzinfo=KL)
    midnight = datetime(2026, 6, 16, 0, 0, tzinfo=KL)
    assert selection.classify_family(rows, NOW, last_second).kind is F.RETURNED
    assert selection.classify_family(rows, NOW, midnight).kind is F.EXPIRED


def test_dates_are_kuala_lumpur_dates_with_a_fixed_offset() -> None:
    # 23:30 UTC is 07:30 the next day in Kuala Lumpur: an approval dated that day is not ahead.
    late_utc = datetime(2026, 6, 14, 23, 30, tzinfo=UTC)
    rows = [v(1, approved_on=TODAY)]
    assert selection.local_date(late_utc) == TODAY
    result = selection.classify_family(rows, late_utc, late_utc + timedelta(hours=2))
    assert result.kind is F.RETURNED
    assert selection.valid_from(TODAY) == datetime(2026, 6, 14, 16, 0, tzinfo=UTC)
    assert selection.valid_until(TODAY) == datetime(2026, 6, 15, 16, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        selection.local_date(datetime(2026, 6, 15, 10, 0))


def test_a_version_taking_effect_inside_a_window_that_crosses_midnight_governs() -> None:
    now = datetime(2026, 6, 15, 22, 0, tzinfo=KL)
    rows = [v(1), v(2, eff=TODAY + DAY)]
    assert selection.classify_family(rows, now, now + timedelta(hours=4)).version == 2
    assert selection.classify_family(rows, now, now + timedelta(hours=1)).version == 1


def _r(kind: FamilyClass, doc: str = "d") -> FamilyResult:
    return FamilyResult(doc, kind, 1 if kind is F.RETURNED else None, 1)


def test_issues_per_family() -> None:
    assert selection.family_issues([_r(F.CLOCK_ISSUE), _r(F.UNCHECKED_ISSUE)]) == (
        CLOCK_ISSUE,
        UNCHECKED_ISSUE,
    )
    assert selection.family_issues([_r(F.INCONSISTENT_ISSUE)]) == (INCONSISTENT_ISSUE,)
    # Every family that would otherwise be returned is past review-by: an issue.
    assert selection.family_issues([_r(F.EXPIRED), _r(F.WITHDRAWN)]) == (ALL_EXPIRED_ISSUE,)
    assert selection.family_issues([_r(F.EXPIRED), _r(F.CLOCK_ISSUE)]) == (
        CLOCK_ISSUE,
        ALL_EXPIRED_ISSUE,
    )
    assert selection.family_issues([_r(F.EXPIRED), _r(F.RETURNED)]) == ()
    assert selection.family_issues([_r(F.WITHDRAWN), _r(F.NOT_IN_FORCE)]) == ()


def test_notes_per_family() -> None:
    assert selection.family_notes([_r(F.EXPIRED), _r(F.RETURNED)]) == (Note(NoteCode.EXPIRED, 1),)
    assert selection.family_notes([_r(F.EXPIRED)]) == ()
    assert selection.family_notes([]) == (Note(NoteCode.NO_DOCUMENTS, 1),)
    assert selection.family_notes(
        [_r(F.WITHDRAWN, "a"), _r(F.WITHDRAWN, "b"), _r(F.NOT_IN_FORCE, "c")]
    ) == (Note(NoteCode.NOT_IN_FORCE, 1), Note(NoteCode.WITHDRAWN, 2))


def test_issue_wording_carries_no_title_and_no_id() -> None:
    assert CLOCK_ISSUE == (
        "knowledge store/run clock disagreement: an approved document family is dated after "
        "this run's clock"
    )
    assert UNCHECKED_ISSUE == (
        "knowledge store: an approved document family has an unchecked approval"
    )


def test_classify_groups_families() -> None:
    rows = [v(1, doc="b-doc"), v(1, doc="a-doc", withdrawn=True), v(2, doc="b-doc", checked=False)]
    assert [(r.document_id, r.kind) for r in selection.classify(rows, NOW, END)] == [
        ("a-doc", F.WITHDRAWN),
        ("b-doc", F.UNCHECKED_ISSUE),
    ]
    with pytest.raises(ValueError):
        selection.classify_family([], NOW, END)
    with pytest.raises(ValueError):
        selection.classify_family([v(1, doc="a-doc"), v(1, doc="b-doc")], NOW, END)


def test_a_version_row_refuses_impossible_facts() -> None:
    with pytest.raises(ValueError):
        v(1, latest="PUBLISHED")
    with pytest.raises(ValueError):  # site and all-sites disagree
        VersionRow("d", 1, None, False, TODAY, TODAY, True, True, True, False, TODAY, "APPROVED", 1)
    with pytest.raises(ValueError):  # approved without a date
        VersionRow("d", 1, None, True, TODAY, TODAY, True, True, True, False, None, "APPROVED", 1)
    with pytest.raises(ValueError):
        v(1, eff=TODAY, rev=TODAY - DAY)
    assert v(1, site="s").applies_to("s")
    assert not v(1, site="s").applies_to("t")
    assert v(1).applies_to("anything")


WALK = (
    "    if any(r.approved_on is None or r.approved_on > today for r in above):\n"
    "        return FamilyResult(family, FamilyClass.CLOCK_ISSUE)\n"
)
UNCHECKED = (
    "    if any(not r.hash_checked for r in above):\n"
    "        return FamilyResult(family, FamilyClass.UNCHECKED_ISSUE)\n"
)
IN_FORCE = "(r for r in rows if r.approved and valid_from(r.effective_date) <= window_end)"
UNTIL = "    return local_midnight(review_by_date + timedelta(days=1))\n"

MUTATIONS = [
    pytest.param(("        and row.hash_checked\n", ""), "unchecked-over-clean", id="no-hash"),
    pytest.param(
        ("        and not row.withdrawn\n", ""),
        "withdrawn-event-below-latest-approved",
        id="withdrawn-event-not-final",
    ),
    pytest.param(
        ("        and row.latest == APPROVED\n", ""),
        "approved-but-latest-draft",
        id="latest-status-ignored",
    ),
    pytest.param(
        ("and row.approved_on <= today\n", "and row.approved_on <= today + timedelta(days=1)\n"),
        "unchecked-not-cleared-by-future-dated",
        id="future-approval-clears",
    ),
    pytest.param(
        (IN_FORCE, IN_FORCE.replace("r.approved and", "r.approved and r.hash_checked and")),
        "unchecked-over-clean",
        id="unchecked-read-as-not-approved",
    ),
    pytest.param(
        (IN_FORCE, IN_FORCE.replace("r.approved and", "r.approved and not r.withdrawn and")),
        "withdrawn-over-clean",
        id="withdrawn-version-does-not-hide",
    ),
    pytest.param(
        (IN_FORCE, IN_FORCE.replace("r.approved and", "r.approved and not r.withdrawn and")),
        "unchecked-withdrawn",
        id="unchecked-withdrawn-read-as-never-approved",
    ),
    pytest.param((WALK, ""), "clock-withdrawn", id="clock-row-dropped"),
    pytest.param(
        (WALK, WALK.replace("for r in above", "for r in candidates")),
        "clock-below-clean",
        id="clock-judged-over-all-candidates",
    ),
    pytest.param(
        (UNCHECKED, UNCHECKED.replace("in above", "in above[:1]")),
        "withdrawn-over-unchecked",
        id="only-governing-version-judged",
    ),
    pytest.param((WALK + UNCHECKED, UNCHECKED + WALK), "clock-and-unchecked-one-issue", id="order"),
    pytest.param(
        ("    if governing.withdrawn:\n", "    if governing.latest == WITHDRAWN:\n"),
        "withdrawn-event-below-latest-approved",
        id="withdrawn-from-latest-only",
    ),
    pytest.param(
        ("        return FamilyResult(family, FamilyClass.WITHDRAWN)\n", "        pass\n"),
        "withdrawn-over-clean",
        id="withdrawn-not-a-note",
    ),
    pytest.param((UNTIL, UNTIL.replace("days=1", "days=2")), "expired", id="review-by-too-long"),
    pytest.param(
        (UNTIL, UNTIL.replace(" + timedelta(days=1)", "")),
        "review-by-today",
        id="review-by-exclusive",
    ),
    pytest.param(
        (
            "        if usable_for(governing.review_by_date, window_end):\n",
            "        if True:\n",
        ),
        "expired",
        id="review-by-not-checked",
    ),
]


@pytest.mark.parametrize(("edit", "case"), MUTATIONS)
def test_mutation_is_caught(edit: tuple[str, str], case: str) -> None:
    _, kind, version = CASES[case]
    real = _run(selection, case)
    assert (real.kind, real.version) == (kind, version)
    flipped = _run(mutated_module(selection, edit), case)
    assert (flipped.kind.value, flipped.version) != (kind.value, version)


def test_mutation_inclusive_review_by_comparison_is_caught() -> None:
    rows = [v(1, rev=TODAY, eff=TODAY - 30 * DAY)]
    midnight = datetime(2026, 6, 16, 0, 0, tzinfo=KL)
    mutant = mutated_module(
        selection, ("    return window_end < valid_until(", "    return window_end <= valid_until(")
    )
    assert selection.classify_family(rows, NOW, midnight).kind is F.EXPIRED
    assert mutant.classify_family(rows, NOW, midnight).kind.value == F.RETURNED.value


def test_mutation_utc_dates_are_caught() -> None:
    late_utc = datetime(2026, 6, 14, 23, 30, tzinfo=UTC)
    rows = [v(1, approved_on=TODAY)]
    mutant = mutated_module(
        selection, ("    return instant.astimezone(KL).date()", "    return instant.date()")
    )
    result = mutant.classify_family(rows, late_utc, late_utc + timedelta(hours=2))
    assert result.kind.value == F.CLOCK_ISSUE.value


def test_mutation_in_force_by_now_is_caught() -> None:
    now = datetime(2026, 6, 15, 22, 0, tzinfo=KL)
    rows = [v(1), v(2, eff=TODAY + DAY)]
    mutant = mutated_module(selection, (IN_FORCE, IN_FORCE.replace("<= window_end", "<= now")))
    assert mutant.classify_family(rows, now, now + timedelta(hours=4)).version == 1


def test_mutation_all_expired_as_note_is_caught() -> None:
    results = [FamilyResult("d", F.EXPIRED)]
    mutant = mutated_module(
        selection,
        (
            "    if FamilyClass.EXPIRED in kinds and FamilyClass.RETURNED not in kinds:\n",
            "    if False:\n",
        ),
    )
    assert selection.family_issues(results) == (ALL_EXPIRED_ISSUE,)
    assert mutant.family_issues(results) == ()
