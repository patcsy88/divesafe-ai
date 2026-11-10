"""ADR 0022 decision 5: who may emit knowledge, when knowledge is window-exempt, and which
categories it may satisfy. Failure conditions first. Every item below is SYNTHETIC test data.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from tests.agent import scripted_llm as sl
from tests.safety.test_evidence_usability import (
    END,
    KNOWLEDGE,
    NOTICE,
    NOW,
    PLAN,
    START,
    H,
    _engine,
    _item,
)

from divesafe.data import (
    TIOMAN_ISLAND,
    DataGovMyWarningConnector,
    FetchResult,
    OpenMeteoMarineConnector,
)
from divesafe.domain import (
    KNOWLEDGE_CATEGORIES,
    DataCategory,
    DataKind,
    DataQuality,
    EvidenceItem,
    Recommendation,
    severity,
)
from divesafe.risk.engine import WINDOW_EXEMPT, _exempt
from divesafe.services import (
    KNOWLEDGE_FROM_OTHER_CONNECTOR_ISSUE,
    NON_KNOWLEDGE_FROM_KNOWLEDGE_CONNECTOR_ISSUE,
    gather_evidence,
)

pytestmark = pytest.mark.safety

R = Recommendation
C = DataCategory
S = timedelta(seconds=1)
KB_ID = "kb:site-guide:2:7"
FAR = NOW + 365 * 24 * H


def _knowledge(
    item_id: str = KB_ID,
    *,
    category: DataCategory = C.LOCAL_GUIDANCE,
    valid_at: datetime = NOW - 10 * H,
    valid_until: datetime | None = FAR,
) -> EvidenceItem:
    """SYNTHETIC knowledge in the record form (no text). `valid_until=None` or a live category
    goes through `model_construct`, as stored or hostile input could."""
    item = _item(
        category=C.LOCAL_GUIDANCE,
        kind=KNOWLEDGE,
        quality=DataQuality.DEGRADED,
        valid_at=valid_at,
        valid_until=FAR,
        item_id=item_id,
    )
    if valid_until is not None and category in KNOWLEDGE_CATEGORIES:
        return item.model_copy(update={"valid_until": valid_until, "category": category})
    return EvidenceItem.model_construct(
        **{**dict(item), "valid_until": valid_until, "category": category}
    )


def _live(item_id: str = "syn-wave", **changes: Any) -> EvidenceItem:
    return _item(kind=DataKind.MODEL, quality=DataQuality.DEGRADED, item_id=item_id, **changes)


# --- the identity filter ----------------------------------------------------------------------


class _Source:
    """SYNTHETIC connector returning fixed items."""

    def __init__(self, name: str, items: Sequence[EvidenceItem]) -> None:
        self.name = name
        self._items = tuple(items)

    async def fetch(
        self, site: object, window_start: datetime, window_end: datetime, now: datetime
    ) -> FetchResult:
        return FetchResult(items=self._items)


class _SubSource(_Source):
    pass


def _gather(connectors: Sequence[Any], knowledge: Any = None) -> Any:
    return asyncio.run(
        gather_evidence(connectors, TIOMAN_ISLAND, START, END, NOW, knowledge_connector=knowledge)
    )


def _kinds_of_knowledge_from_elsewhere() -> list[EvidenceItem]:
    """Every way a non-knowledge connector might carry knowledge: the kind, the prefix, both,
    and the prefix in another case."""
    return [
        _knowledge("syn-site"),  # KNOWLEDGE kind, ordinary id
        _live("kb:site-guide:2:8"),  # ordinary kind, reserved id
        _live("KB:site-guide:2:9"),  # reserved id, upper case
        _knowledge("kb:site-guide:2:10"),  # both
    ]


@pytest.mark.parametrize("knowledge_connector", ["none", "another"])
def test_knowledge_from_any_other_connector_is_dropped_with_the_fixed_issue(
    knowledge_connector: str,
) -> None:
    live = _live()
    other = _Source("syn-live", [live, *_kinds_of_knowledge_from_elsewhere()])
    real_kb = _Source("syn-kb", [_knowledge()])
    kb = None if knowledge_connector == "none" else real_kb
    connectors = [other] if kb is None else [other, kb]
    result = _gather(connectors, kb)
    from_other = [e for e in result.items if e.id != KB_ID]
    assert from_other == [live]
    assert f"syn-live: {KNOWLEDGE_FROM_OTHER_CONNECTOR_ISSUE}" in result.issues
    # The real knowledge connector's own item is untouched.
    assert (KB_ID in {e.id for e in result.items}) is (kb is not None)


@pytest.mark.parametrize("item_index", range(4))
def test_each_form_of_knowledge_from_another_connector_is_dropped_on_its_own(
    item_index: int,
) -> None:
    item = _kinds_of_knowledge_from_elsewhere()[item_index]
    result = _gather([_Source("syn-live", [_live(), item])])
    assert [e.id for e in result.items] == ["syn-wave"]
    assert result.issues == (f"syn-live: {KNOWLEDGE_FROM_OTHER_CONNECTOR_ISSUE}",)


def test_identity_not_name_and_not_isinstance() -> None:
    real = _Source("knowledge", [_knowledge()])
    same_name = _Source("knowledge", [_knowledge("kb:site-guide:2:8")])
    subclass = _SubSource("knowledge-sub", [_knowledge("kb:site-guide:2:9")])
    result = _gather([real, same_name, subclass], real)
    assert [e.id for e in result.items] == [KB_ID]
    issue = KNOWLEDGE_FROM_OTHER_CONNECTOR_ISSUE
    assert f"knowledge: {issue}" in result.issues
    assert f"knowledge-sub: {issue}" in result.issues


def test_the_knowledge_connector_may_emit_only_knowledge_with_a_kb_id() -> None:
    kb = _Source(
        "syn-kb",
        [
            _knowledge(),
            _live("syn-wave-from-kb"),  # not knowledge
            _knowledge("syn-plain-id"),  # knowledge without a kb: id
            _live("kb:site-guide:2:8"),  # kb: id without the kind
        ],
    )
    result = _gather([kb], kb)
    assert [e.id for e in result.items] == [KB_ID]
    assert result.issues == (f"syn-kb: {NON_KNOWLEDGE_FROM_KNOWLEDGE_CONNECTOR_ISSUE}",)


def test_a_clean_result_adds_no_issue_in_either_direction() -> None:
    other = _Source("syn-live", [_live()])
    kb = _Source("syn-kb", [_knowledge()])
    result = _gather([other, kb], kb)
    assert sorted(e.id for e in result.items) == sorted([KB_ID, "syn-wave"])
    assert result.issues == ()


def test_the_issue_text_is_fixed_and_carries_no_id_title_or_content() -> None:
    item = _knowledge("syn-secret-title-marker")
    result = _gather([_Source("syn-live", [item])])
    assert all("secret-title-marker" not in i for i in result.issues)
    for text in (
        KNOWLEDGE_FROM_OTHER_CONNECTOR_ISSUE,
        NON_KNOWLEDGE_FROM_KNOWLEDGE_CONNECTOR_ISSUE,
    ):
        assert text.startswith("evidence.knowledge_refused: ")


class _Smuggler:
    """SYNTHETIC: an ordinary connector that also emits a knowledge item."""

    name = "syn-smuggler"

    async def fetch(
        self, site: object, window_start: datetime, window_end: datetime, now: datetime
    ) -> FetchResult:
        return FetchResult(
            items=(
                EvidenceItem(
                    id="syn-site",
                    category=C.SITE_INFORMATION,
                    source="synthetic",
                    retrieved_at=now,
                    valid_at=now,
                    valid_until=now + timedelta(days=30),
                    is_forecast=False,
                    data_kind=DataKind.KNOWLEDGE,
                    value={},
                ),
            )
        )


def test_the_pipeline_drops_it_records_the_issue_and_cannot_give_better_than_insufficient() -> None:
    connectors = [
        OpenMeteoMarineConnector(sl._Getter(sl.MARINE)),
        DataGovMyWarningConnector(sl._Getter(sl.WARNINGS)),
        _Smuggler(),
    ]
    record = sl.run_pipeline(sl.fake(recommendation="GO"), connectors=connectors)
    assert "syn-site" not in {e.id for e in record.evidence}
    assert f"syn-smuggler: {KNOWLEDGE_FROM_OTHER_CONNECTOR_ISSUE}" in record.evidence_issues
    assert severity(record.final_recommendation) >= severity(R.INSUFFICIENT_EVIDENCE)


# --- _exempt: strict for knowledge, unchanged for notices -------------------------------------


@pytest.mark.parametrize(
    ("valid_at", "valid_until", "exempt"),
    [
        (NOW - 10 * H, END, False),  # review-by equals the window end
        (NOW - 10 * H, END - S, False),  # review-by inside the window
        (NOW - 10 * H, START - S, False),  # review-by before the dive
        (NOW - 10 * H, END + S, True),  # one second after the window end
        (END + S, FAR, False),  # takes effect after the dive
        (END, FAR, True),  # takes effect exactly at the window end
        (NOW - 10 * H, None, False),  # no review-by date (model_construct only)
    ],
    ids=["until-eq-end", "until-in", "until-before", "until-after", "at-after", "at-end", "none"],
)
def test_knowledge_exemption_is_strict(
    valid_at: datetime, valid_until: datetime | None, exempt: bool
) -> None:
    assert _exempt(_knowledge(valid_at=valid_at, valid_until=valid_until), START, END) is exempt


@pytest.mark.parametrize(
    ("valid_until", "exempt"),
    [(None, True), (START, True), (END, True), (START - S, False)],
    ids=["none", "until-eq-start", "until-eq-end", "until-before-start"],
)
def test_the_notice_exemption_is_unchanged(valid_until: datetime | None, exempt: bool) -> None:
    notice = _item(
        category=C.MARINE_WARNINGS,
        kind=NOTICE,
        quality=DataQuality.DEGRADED,
        valid_at=NOW - 10 * H,
        valid_until=valid_until,
    )
    assert _exempt(notice, START, END) is exempt


def _verdict(item: EvidenceItem) -> Recommendation:
    engine = _engine({C.LOCAL_GUIDANCE}, degraded_may_support_go=True)
    return engine.assess(PLAN, [item], NOW).recommendation


@pytest.mark.parametrize(
    ("valid_at", "valid_until", "expected"),
    [
        # Mid-dive effect so the item cannot cover the window on its own: only exemption helps.
        (START + 60 * S, END, R.INSUFFICIENT_EVIDENCE),
        (START + 60 * S, END + S, R.GO),
        (END + S, FAR, R.INSUFFICIENT_EVIDENCE),
        (START + 60 * S, None, R.INSUFFICIENT_EVIDENCE),
    ],
    ids=["until-eq-end", "until-after-end", "at-after-end", "none"],
)
def test_a_stored_knowledge_item_reassessed_by_the_engine_honours_the_strict_window(
    valid_at: datetime, valid_until: datetime | None, expected: Recommendation
) -> None:
    """Record-form items re-assessed without the connector's filter (decision 5)."""
    assert _verdict(_knowledge(valid_at=valid_at, valid_until=valid_until)) == expected


def test_knowledge_needs_a_review_by_date_and_its_own_categories() -> None:
    with pytest.raises(ValidationError, match="knowledge requires valid_until"):
        _item(category=C.LOCAL_GUIDANCE, kind=KNOWLEDGE, valid_at=NOW - H, valid_until=None)
    for category in C:
        if category in KNOWLEDGE_CATEGORIES:
            _item(category=category, kind=KNOWLEDGE, valid_at=NOW - H, valid_until=FAR)
            continue
        with pytest.raises(ValidationError, match="knowledge is valid only as"):
            _item(category=category, kind=KNOWLEDGE, valid_at=NOW - H, valid_until=FAR)
    assert frozenset({C.SITE_INFORMATION, C.LOCAL_GUIDANCE}) == KNOWLEDGE_CATEGORIES


# --- historical_observations ------------------------------------------------------------------


def test_knowledge_no_longer_satisfies_historical_observations() -> None:
    assert C.HISTORICAL_OBSERVATIONS not in WINDOW_EXEMPT[DataKind.KNOWLEDGE]
    with pytest.raises(ValidationError, match="knowledge is valid only as"):
        _item(category=C.HISTORICAL_OBSERVATIONS, kind=KNOWLEDGE, valid_at=NOW - H, valid_until=FAR)
    # A copy that skipped the validator, covering the window and long in force, is ignored.
    smuggled = _knowledge(category=C.HISTORICAL_OBSERVATIONS, valid_at=START - H)
    engine = _engine({C.HISTORICAL_OBSERVATIONS}, degraded_may_support_go=True)
    result = engine.assess(PLAN, [smuggled], NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    assert any(
        r.rationale == "No 'historical_observations' evidence is available."
        for r in result.rule_results
    )
    # The same item as a real observation satisfies the category (the test has a positive side).
    observed = _item(
        category=C.HISTORICAL_OBSERVATIONS,
        kind=DataKind.OBSERVATION,
        quality=DataQuality.DEGRADED,
        valid_at=START,
        valid_until=END,
    )
    assert engine.assess(PLAN, [observed], NOW).recommendation == R.GO
