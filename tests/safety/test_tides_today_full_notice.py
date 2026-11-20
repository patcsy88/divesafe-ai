"""ADR 0029 decision 11 (owner decision, 2026-11-19): a tides.today item never reaches the user
view without the provider's FULL notice. The notice is untrusted provider text: shown as plain
text, selected by the item's source id, bounded, never prompted, never traced, never logged.

All fixtures are SYNTHETIC (tests/fixtures/README.md).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.agent import scripted_llm as sl
from tests.safety import trace_wire as tw
from tests.safety.kb_fakes import mutated_module

from divesafe.agents.prompts import RECORD_ONLY_VALUE_KEYS, evidence_payload
from divesafe.api import schemas as schemas_module
from divesafe.api.schemas import AssessmentView
from divesafe.data import TIOMAN_ISLAND
from divesafe.data import tides_today as tt
from divesafe.domain import AssessmentRecord, DataCategory, DivePlan
from divesafe.models import FakeProvider
from divesafe.observability import start_collecting
from divesafe.orchestration import assess_dive
from divesafe.risk import EvidencePolicy, RiskRulesEngine, placeholder_rules

pytestmark = pytest.mark.safety

NOTE = (
    "Text supplied by the data provider, shown as received. It is the provider's own copyright and "
    "licence notice, shown because this assessment retrieved tides.today data (that data may have "
    "been withheld; see Evidence, if your key can read it). DiveSafe has not checked it. "
    "It does not validate the data, does not describe the tide at the dive site, and does not "
    "change the result."
)
TIOMAN_LOCATION = tt.LOCATIONS[TIOMAN_ISLAND.id]
MISSING = "provider notice missing for a tides.today item"
LOOKALIKE_SOURCES = (
    "kb:tides-today-prediction",
    "tides-today-prediction-x",
    " tides-today-prediction",
    "phn-etide-prediction",
    "other-source",
)

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures/tides_today_teluk_tekek_synthetic.json").read_text()
)
NOW = datetime(2026, 11, 17, 1, 0, tzinfo=UTC)
START = datetime(2026, 11, 18, 0, 0, tzinfo=UTC)
END = START + timedelta(hours=4)
MIDNIGHT = datetime(2026, 11, 18, 15, 30, tzinfo=UTC)  # 23:30 MYT: touches two local days
# 1,900 printable characters, with markup, that no 300-character cut could keep whole.
LONG_NOTICE = "NOTICE-START <b>bold</b> & &amp; " + "tide text " * 185 + "NOTICE-END"
OTHER_NOTICE = LONG_NOTICE.replace("NOTICE-END", "NOTICE-END-2")
BOTH = tuple(sorted({LONG_NOTICE.strip(), OTHER_NOTICE.strip()}))
assert 1800 < len(LONG_NOTICE) <= tt.MAX_NOTICE_CHARS


class _Getter:
    def __init__(self, notice: str, two_days: bool = False) -> None:
        self.payloads = [
            copy.deepcopy(FIXTURE[k]) for k in ("location", "tide_extremes", "ephemeris")
        ]
        if two_days:
            _, extremes, ephemeris = self.payloads
            extremes["meta"]["params"]["filter.dateTime"] = "$btw:2026-11-17,2026-11-20"
            second = copy.deepcopy(ephemeris["items"][0])
            second.update(
                id=501,
                date="2026-11-19",
                sunrise="2026-11-18T22:51:00.000Z",
                sunset="2026-11-19T10:46:00.000Z",
            )
            ephemeris["items"].append(second)
            ephemeris["meta"]["totalItems"] = 2
            ephemeris["meta"]["params"]["filter.date"] = "$btw:2026-11-18,2026-11-19"
        self.payloads[0]["data"]["source"]["copyright"] = notice
        self.n = 0

    async def get_json(self, url: str, params: object) -> object:
        self.n += 1
        return self.payloads[self.n - 1]


def _record(
    notice: str = LONG_NOTICE,
    extra_connectors: list[Any] | None = None,
    two_days: bool = False,
) -> AssessmentRecord:
    """`two_days`: a dive across local midnight, so the record holds two items (one per day)
    that carry the same notice."""
    engine = RiskRulesEngine(
        list(placeholder_rules()),
        EvidencePolicy(frozenset({DataCategory.TIDES}), timedelta(hours=1)),
        "synthetic",
    )
    return asyncio.run(
        assess_dive(
            assessment_id="a-1",
            plan=DivePlan(
                site_id=TIOMAN_ISLAND.id,
                planned_start=MIDNIGHT if two_days else START,
                planned_duration_minutes=60,
                max_depth_m=18,
            ),
            site=TIOMAN_ISLAND,
            connectors=[
                tt.TidesTodayConnector(_Getter(notice, two_days)),
                *(extra_connectors or []),
            ],
            engine=engine,
            now=NOW,
            egress_basis=None,
            model_host=None,
        )
    )


def _withheld_record() -> AssessmentRecord:
    """Two sources send the same ids with different text: the items are withheld evidence (a real
    conflict, so the record validates); the notice must still be shown."""
    record = _record(extra_connectors=[tt.TidesTodayConnector(_Getter(OTHER_NOTICE))])
    assert record.withheld_evidence and not record.evidence
    return record


def _with_notices(record: AssessmentRecord, *notices: str) -> AssessmentRecord:
    """The same items (so the record still validates), each with the next notice."""
    items = tuple(
        e.model_copy(update={"value": {**e.value, "provider_notice": n}})
        for e, n in zip(record.evidence, notices, strict=True)
    )
    return record.model_copy(update={"evidence": items})


def _other_source(record: AssessmentRecord, notice: str, source: str) -> AssessmentRecord:
    """A different source whose value happens to carry a `provider_notice` key."""
    items = tuple(
        e.model_copy(update={"source": source, "value": {**e.value, "provider_notice": notice}})
        for e in record.evidence
    )
    return record.model_copy(update={"evidence": items})


def _other_category(record: AssessmentRecord, category: DataCategory) -> AssessmentRecord:
    items = tuple(e.model_copy(update={"category": category}) for e in record.evidence)
    return record.model_copy(update={"evidence": items})


def _fake_item(text: Any, source: str = tt.SOURCE, category: Any = DataCategory.TIDES) -> Any:
    return SimpleNamespace(source=source, category=category, value={"provider_notice": text})


def _fake_record(*items: Any) -> Any:
    return SimpleNamespace(evidence=items, withheld_evidence=())


def _log_misses(view_cls: Any) -> list[str]:
    """Building either view must log nothing that holds the notice: not the message, not an
    `extra` field, not any other record attribute."""
    record = _record()
    held = _withheld_record()
    seen: list[logging.LogRecord] = []

    class _Catch(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    package, handler = logging.getLogger("divesafe"), _Catch(logging.DEBUG)
    previous = package.level
    package.addHandler(handler)
    package.setLevel(logging.DEBUG)
    try:
        for built in (record, held):
            view_cls.of(built)
            view_cls.withheld(built)
    finally:
        package.removeHandler(handler)
        package.setLevel(previous)
    bad = []
    for entry in seen:
        text = entry.getMessage() + " ".join(str(v) for v in entry.__dict__.values())
        if "NOTICE-START" in text or "NOTICE-END" in text or "tide text" in text:
            bad.append("the notice reached a log record")
            break
    return bad


def _field_misses(view_cls: Any) -> list[str]:
    """The fixed note, the other fields, the cap and the missing-notice line."""
    bad: list[str] = []
    record = _record()
    other = _with_notices(record, "A different notice " + "d" * 300)
    held = _withheld_record()
    for label, view in (
        ("normal", view_cls.of(record)),
        ("create-only", view_cls.withheld(record)),
        ("withheld items", view_cls.of(held)),
        ("create-only, withheld items", view_cls.withheld(held)),
    ):
        if view.provider_notice_note != NOTE:
            bad.append(f"{label}: the fixed note is missing or changed")
        if not view.provider_notices:
            bad.append(f"{label}: no notice")
        if view.provider_notice_missing:
            bad.append(f"{label}: the missing flag is set for a good notice")
        fields = (
            view.outcome_note,
            view.status_note,
            view.agent_note,
            view.knowledge_note,
            view.confidence_note,
            view.notice,
            *view.attributions,
        )
        if any("NOTICE-START" in f or "tide text" in f for f in fields):
            bad.append(f"{label}: the notice leaked into another view field")
    # A view with neither a provider notice nor the flag carries no note and no flag.
    for label, view in (
        ("normal", view_cls.of(_other_source(record, "x", "other-source"))),
        ("create-only", view_cls.withheld(_other_source(record, "x", "other-source"))),
    ):
        if view.provider_notice_note != "" or view.provider_notice_missing:
            bad.append(f"{label}: a view without a tides.today item has a note or the flag")
    keep = {"provider_notices", "record"}
    a, b = view_cls.of(record).model_dump(exclude=keep), view_cls.of(other).model_dump(exclude=keep)
    if a != b:
        bad.append("the view differs by more than the notices when only the notice differs")
    a = view_cls.withheld(record).model_dump(exclude=keep)
    if a != view_cls.withheld(other).model_dump(exclude=keep):
        bad.append("the create-only view differs by more than the notices")
    # Missing, blank, not a string, not printable: no text, the flag, and the note.
    days = _record(two_days=True)
    for broken in ("", "   ", None, 7, ["x"], "a\nb", "a\x07b", "x\u202ey", "a\u200bb"):
        for label, view in (
            ("normal", view_cls.of(_with_notices(days, broken, broken))),
            ("create-only", view_cls.withheld(_with_notices(days, broken, broken))),
        ):
            if tuple(view.provider_notices) != () or view.provider_notice_missing is not True:
                bad.append(f"{label}: a notice of {broken!r} did not set only the missing flag")
            if view.provider_notice_note != NOTE:
                bad.append(f"{label}: the note is missing when only the flag is set")
    both = view_cls.of(_with_notices(days, LONG_NOTICE, 7))
    if tuple(both.provider_notices) != (LONG_NOTICE.strip(),) or not both.provider_notice_missing:
        bad.append("a good notice and a broken one did not give the text and the flag")
    # A provider notice that reads like the missing sentence is provider text, not the flag.
    same = view_cls.of(_with_notices(days, MISSING, MISSING))
    if tuple(same.provider_notices) != (MISSING,) or same.provider_notice_missing:
        bad.append("provider text equal to the missing sentence was treated as the flag")
    # The cap, through the real view, with the cap lowered so two real days are enough.
    module = sys.modules[view_cls.__module__]
    original = module.MAX_PROVIDER_NOTICES_SHOWN
    module.MAX_PROVIDER_NOTICES_SHOWN = 1
    try:
        two = view_cls.of(_with_notices(days, "a " + "a" * 50, "b " + "b" * 50))
        if two.provider_notices != ("a " + "a" * 50,) or two.provider_notices_not_shown != 1:
            bad.append("the cap or the count of notices not shown is wrong")
    finally:
        module.MAX_PROVIDER_NOTICES_SHOWN = original
    texts = [f"notice {n:02d} " + "q" * (n * 7) for n in range(6)]
    listed, hidden, _ = module._provider_notices(
        _fake_record(*(_fake_item(t) for t in reversed(texts)))
    )
    if tuple(listed) != tuple(t.strip() for t in texts[:4]) or hidden != 2:
        bad.append("more than four notices are not cut to the first four with a count")
    exact, none, _ = module._provider_notices(_fake_record(*(_fake_item(t) for t in texts[:4])))
    if len(exact) != 4 or none != 0:
        bad.append("exactly four notices are cut")
    return bad


def full_notice_misses(view_cls: Any) -> list[str]:
    bad: list[str] = []
    record = _record()
    if any(len(a) > 300 for a in view_cls.of(record).attributions):
        bad.append("an attribution is longer than the cut")
    held = _withheld_record()
    for label, shown, want in (
        ("normal", view_cls.of(record), (LONG_NOTICE.strip(),)),
        ("withheld evidence", view_cls.of(held), BOTH),
        ("create-only", view_cls.withheld(record), (LONG_NOTICE.strip(),)),
        ("create-only, withheld items", view_cls.withheld(held), BOTH),
    ):
        if tuple(shown.provider_notices) != want:
            bad.append(f"{label}: the full notice is not shown whole")
        if tt.ATTRIBUTION not in shown.attributions:
            bad.append(f"{label}: the short attribution is gone")
    for source in LOOKALIKE_SOURCES:
        other = view_cls.of(_other_source(record, "OTHER SOURCE TEXT", source))
        if other.provider_notices or other.provider_notice_missing:
            bad.append(f"the provider_notice key of source {source!r} was shown")
    wind_view = view_cls.of(_other_category(record, DataCategory.WIND))
    wind = wind_view.provider_notices or wind_view.provider_notice_missing
    if wind:
        bad.append("a notice of a non-tide item was shown")
    # View-level bound and strip, whatever the stored value is.
    odd = _with_notices(_record(two_days=True), "  padded  ", "x" * (tt.MAX_NOTICE_CHARS + 50))
    if tuple(view_cls.of(odd).provider_notices) != tuple(
        sorted(["padded", "x" * tt.MAX_NOTICE_CHARS])
    ):
        bad.append("the notice is not stripped and cut at exactly the stored bound")
    days = _record(two_days=True)
    if len(days.evidence) != 2 or len(view_cls.of(days).provider_notices) != 1:
        bad.append("duplicates were not collapsed")
    mixed = _with_notices(days, "B notice " + "b" * 400, LONG_NOTICE)
    notices = tuple(view_cls.of(mixed).provider_notices)
    if notices != tuple(sorted(notices)) or len(notices) != 2:
        bad.append("notices are not sorted and distinct")
    bad += _field_misses(view_cls)
    bad += _log_misses(view_cls)
    return bad


def test_a_tides_today_item_never_reaches_the_user_view_without_its_full_notice() -> None:
    assert full_notice_misses(AssessmentView) == []


def test_the_view_shows_the_full_notice_uncut_and_the_attribution_stays_short() -> None:
    record = _record()
    assert all(e.value["provider_notice"] == LONG_NOTICE.strip() for e in record.evidence)
    view = AssessmentView.of(record)
    assert view.provider_notices == (LONG_NOTICE.strip(),)
    assert len(view.provider_notices[0]) > 1800
    assert view.attributions == (tt.ATTRIBUTION,)
    assert tt.ATTRIBUTION not in view.provider_notices[0]
    body = json.loads(view.model_dump_json())
    assert body["provider_notices"] == [LONG_NOTICE.strip()]


def test_the_notice_is_shown_for_a_create_only_key_too() -> None:
    view = AssessmentView.withheld(_record())
    assert view.decision_withheld
    assert view.provider_notices == (LONG_NOTICE.strip(),)


def test_the_notice_is_shown_when_the_items_are_withheld_evidence() -> None:
    view = AssessmentView.of(_withheld_record())
    assert view.provider_notices == BOTH


def test_another_sources_provider_notice_key_is_not_shown() -> None:
    record = _other_source(_record(), "OTHER SOURCE TEXT", "other-source")
    view = AssessmentView.of(record)
    assert view.provider_notices == ()


def test_duplicates_collapse_and_the_order_is_stable() -> None:
    days = _record(two_days=True)
    assert len(days.evidence) == 2
    assert AssessmentView.of(days).provider_notices == (LONG_NOTICE.strip(),)
    a, b = "A second notice " + "a" * 300, "Z last " + "z" * 300
    forward = _with_notices(days, b, a)
    backward = _with_notices(days, a, b)
    assert AssessmentView.of(forward).provider_notices == (a, b)
    assert AssessmentView.of(backward).provider_notices == (a, b)


@pytest.mark.parametrize(
    "markup",
    [
        "<script>alert(1)</script><img src=x onerror=1>",
        '&lt;b&gt; {{7*7}} ${x} [link](http://x) <a href="x">y</a>',
    ],
)
def test_markup_stays_plain_text_in_the_json(markup: str) -> None:
    record = _record()
    items = tuple(
        e.model_copy(update={"value": {**e.value, "provider_notice": markup}})
        for e in record.evidence
    )
    view = AssessmentView.of(record.model_copy(update={"evidence": items}))
    assert view.provider_notices == (markup.strip(),)
    assert json.loads(view.model_dump_json())["provider_notices"] == [markup.strip()]


@pytest.mark.parametrize(
    "hostile", ["a\x07b\x1b[31mred", "x\u202ey", "a\u200bb", "line\nbreak", "tab\there", "\x00"]
)
def test_a_notice_that_is_not_printable_is_not_shown_and_the_missing_flag_is_set(
    hostile: str,
) -> None:
    """The connector refuses a notice that is not printable; a stored one that is not is not shown
    (the view does not clean it) and the missing flag is set instead."""
    record = _record()
    items = tuple(
        e.model_copy(update={"value": {**e.value, "provider_notice": hostile}})
        for e in record.evidence
    )
    view = AssessmentView.of(record.model_copy(update={"evidence": items}))
    assert view.provider_notices == ()
    assert view.provider_notice_missing is True
    assert json.loads(view.model_dump_json())["provider_notices"] == []


@pytest.mark.parametrize(
    "text",
    [
        "plain",
        "with markup <b>",
        "x" * tt.MAX_NOTICE_CHARS,
        "  padded  ",
        "",
        "   ",
        "a\nb",
        "a\tb",
        "a\x07b",
        "x\u202ey",
        "a\u200bb",
        "x" * (tt.MAX_NOTICE_CHARS + 1),
    ],
)
def test_the_view_agrees_with_the_connector_except_that_it_cuts_an_overlong_notice(
    text: str,
) -> None:
    """The same text through the connector's own notice check and through the view.

    They agree on every case but one, by design: a notice longer than `MAX_NOTICE_CHARS` is
    refused by the connector, while the view (defensively, for a stored value) cuts it to exactly
    that bound and shows it, without the missing flag.
    """
    location = {
        "data": {
            "id": TIOMAN_LOCATION.id,
            "name": TIOMAN_LOCATION.name,
            "lat": str(TIOMAN_LOCATION.latitude),
            "lng": str(TIOMAN_LOCATION.longitude),
            "region": {"timezone": tt.TIMEZONE_NAME},
            "source": {
                "datumType": tt.ACCEPTED_DATUM,
                "source": tt.ACCEPTED_UPSTREAM,
                "copyright": text,
            },
        }
    }
    try:
        tt._check_location(location, TIOMAN_LOCATION)  # noqa: SLF001
        connector_accepts = True
    except tt.ConnectorResponseError:
        connector_accepts = False
    record = _record()
    items = tuple(
        e.model_copy(update={"value": {**e.value, "provider_notice": text}})
        for e in record.evidence
    )
    view = AssessmentView.of(record.model_copy(update={"evidence": items}))
    if len(text) > tt.MAX_NOTICE_CHARS:
        assert connector_accepts is False
        assert view.provider_notices == ("x" * tt.MAX_NOTICE_CHARS,)
        assert view.provider_notice_missing is False
        return
    assert connector_accepts == bool(view.provider_notices)
    assert connector_accepts == (not view.provider_notice_missing)


def test_the_fixed_note_is_pinned_word_for_word() -> None:
    assert schemas_module.PROVIDER_NOTICE_NOTE == NOTE
    assert AssessmentView.of(_record()).provider_notice_note == NOTE
    assert AssessmentView.withheld(_record()).provider_notice_note == NOTE
    assert not hasattr(schemas_module, "PROVIDER_NOTICE_MISSING")
    assert schemas_module.MAX_PROVIDER_NOTICES_SHOWN == 4
    # No tides.today item: no note and no flag.
    bare = AssessmentView.of(_other_source(_record(), "x", "other-source"))
    assert bare.provider_notice_note == "" and bare.provider_notice_missing is False


def test_the_attribution_points_to_the_assessment_page_and_stays_short() -> None:
    assert "Provider notice shown on the assessment page." in tt.ATTRIBUTION
    assert "provider_notice" not in tt.ATTRIBUTION
    assert len(tt.ATTRIBUTION) <= 300


def test_the_notice_is_cut_defensively_at_exactly_the_stored_bound() -> None:
    record = _record()
    too_long = "x" * (tt.MAX_NOTICE_CHARS + 50)
    items = tuple(
        e.model_copy(update={"value": {**e.value, "provider_notice": too_long}})
        for e in record.evidence
    )
    shown = AssessmentView.of(record.model_copy(update={"evidence": items})).provider_notices
    assert shown == ("x" * tt.MAX_NOTICE_CHARS,)
    assert schemas_module._MAX_PROVIDER_NOTICE_CHARS == tt.MAX_NOTICE_CHARS  # noqa: SLF001
    assert schemas_module._MAX_PROVIDER_NOTICE_CHARS > schemas_module._MAX_ATTRIBUTION_CHARS  # noqa: SLF001


def test_a_blank_or_non_string_notice_sets_the_missing_flag() -> None:
    record = _record()
    for bad in ("   ", "", None, 7, ["x"], {"a": "b"}):
        items = tuple(
            e.model_copy(update={"value": {**e.value, "provider_notice": bad}})
            for e in record.evidence
        )
        view = AssessmentView.of(record.model_copy(update={"evidence": items}))
        assert view.provider_notices == ()
        assert view.provider_notice_missing is True
        assert view.provider_notice_note == NOTE


def test_provider_text_equal_to_the_missing_sentence_stays_provider_text() -> None:
    record = _record()
    items = tuple(
        e.model_copy(update={"value": {**e.value, "provider_notice": MISSING}})
        for e in record.evidence
    )
    view = AssessmentView.of(record.model_copy(update={"evidence": items}))
    assert view.provider_notices == (MISSING,)
    assert view.provider_notice_missing is False


def test_the_web_copies_of_the_fixed_wording_equal_the_api_constants() -> None:
    web = (Path(__file__).parents[2] / "web/components/ProviderNotices.ts").read_text("utf8")
    assert json.dumps(schemas_module.PROVIDER_NOTICE_NOTE, ensure_ascii=False) in web
    assert json.dumps(MISSING) in web


def test_the_view_selects_by_the_source_id_constant() -> None:
    assert schemas_module.tides_today.SOURCE == tt.SOURCE
    assert schemas_module._PROVIDER_NOTICE_KEY in RECORD_ONLY_VALUE_KEYS  # noqa: SLF001
    assert frozenset({"provider_notice"}) == RECORD_ONLY_VALUE_KEYS


# --- the notice never reaches a prompt, a span or a log ----------------------------------------


def test_the_full_notice_never_reaches_a_prompt_payload() -> None:
    record = _record()
    sent = json.dumps(evidence_payload(list(record.evidence)))
    assert "NOTICE-START" not in sent and "NOTICE-END" not in sent
    assert all("provider_notice" not in p["value"] for p in evidence_payload(list(record.evidence)))
    assert tt.ATTRIBUTION in sent


def _exported_body(monkeypatch: pytest.MonkeyPatch, *, agents: bool) -> tuple[str, Any, Any]:
    from divesafe.api import trace_export

    provider = FakeProvider(sl.scripted(), model="model-x") if agents else None

    async def run() -> Any:
        collector = start_collecting()
        extra: dict[str, Any] = (
            {
                "provider": provider,
                "clock": lambda: NOW,
                "egress_basis": "declared_local",
                "model_host": "localhost",
            }
            if agents
            else {"egress_basis": None, "model_host": None}
        )
        record = await assess_dive(
            assessment_id="a-1",
            plan=DivePlan(
                site_id=TIOMAN_ISLAND.id,
                planned_start=START,
                planned_duration_minutes=60,
                max_depth_m=18,
            ),
            site=TIOMAN_ISLAND,
            connectors=[tt.TidesTodayConnector(_Getter(LONG_NOTICE))],
            engine=RiskRulesEngine(
                list(placeholder_rules()),
                EvidencePolicy(frozenset({DataCategory.TIDES}), timedelta(hours=1)),
                "synthetic",
            ),
            now=NOW,
            **extra,
        )
        return collector.finish("a-1", "stored", request_id=None), record

    trace, record = asyncio.run(run())
    with tw.installed_wire(monkeypatch) as wire:
        export = wire.build(module=trace_export)
        export.sink.emit(trace)
        assert export.shutdown(5.0)
        body = wire.body_text()
        names = {span.name for span in wire.spans()}
    assert wire.sent, "nothing was exported; the check would pass for no reason"
    assert record.evidence
    return body, names, provider


@pytest.mark.parametrize("agents", [False, True])
def test_no_span_attribute_carries_the_notice(
    monkeypatch: pytest.MonkeyPatch, agents: bool
) -> None:
    body, names, provider = _exported_body(monkeypatch, agents=agents)
    assert "NOTICE-START" not in body and "NOTICE-END" not in body and "tide text" not in body
    if agents:
        assert provider.requests, "no model call was made; the check would pass for no reason"
        assert len(names) > 1, "no model-call spans were exported"
        sent = "\n".join(str(r) for r in provider.requests)
        assert "NOTICE-START" not in sent and "tide text" not in sent
        assert tt.ATTRIBUTION[:40] in sent or "tides.today" in sent


def test_building_the_view_logs_nothing() -> None:
    assert _log_misses(AssessmentView) == []


def test_the_notice_is_in_no_other_view_field_and_changes_nothing_else() -> None:
    assert _field_misses(AssessmentView) == []


# --- mutation checks: each mutant of the view must be caught ----------------------------------

VIEW_MUTANTS = [
    pytest.param(
        [("            provider_notices=notices,\n", "")],
        id="field-not-filled",
    ),
    pytest.param(
        [("            provider_notices=notices,\n", "            provider_notices=(),\n")],
        id="field-empty",
    ),
    pytest.param(
        [
            (
                "notice.strip()[:_MAX_PROVIDER_NOTICE_CHARS]",
                "notice.strip()[:_MAX_ATTRIBUTION_CHARS]",
            )
        ],
        id="cut-at-300",
    ),
    pytest.param(
        [("notice.strip()[:_MAX_PROVIDER_NOTICE_CHARS]", "notice.strip()")],
        id="no-cut-at-all",
    ),
    pytest.param(
        [("item.source != tides_today.SOURCE or", "item.source == tides_today.SOURCE or")],
        id="wrong-source",
    ),
    pytest.param(
        [("item.source != tides_today.SOURCE or ", "")],
        id="no-source-check",
    ),
    pytest.param(
        [("item.source != tides_today.SOURCE or", "tides_today.SOURCE not in item.source or")],
        id="source-by-in",
    ),
    pytest.param(
        [
            (
                "item.source != tides_today.SOURCE or",
                "not item.source.strip().endswith(tides_today.SOURCE) or",
            )
        ],
        id="source-by-endswith",
    ),
    pytest.param(
        [
            (
                "item.source != tides_today.SOURCE or",
                "not item.source.startswith(tides_today.SOURCE) or",
            )
        ],
        id="source-by-startswith",
    ),
    pytest.param(
        [(" or item.category is not DataCategory.TIDES:", ":")],
        id="no-category-check",
    ),
    pytest.param(
        [
            (
                "for item in (*record.evidence, *record.withheld_evidence):",
                "for item in record.evidence:",
            )
        ],
        id="without-withheld-items",
    ),
    pytest.param(
        [("found: set[str] = set()", "found: list[str] = []"), ("found.add(", "found.append(")],
        id="not-deduplicated",
    ),
    pytest.param([("ordered = sorted(found)", "ordered = list(found)")], id="not-sorted"),
    pytest.param(
        [("notice.strip()[:_MAX", "notice[:_MAX")],
        id="not-stripped",
    ),
    pytest.param(
        [(" and notice.strip() and", " and")],
        id="blank-accepted",
    ),
    pytest.param([(" and notice.isprintable()", "")], id="printable-check-removed"),
    pytest.param([(" and notice.isprintable()", " and notice.isascii()")], id="printable-by-ascii"),
    pytest.param(
        [
            (
                "    return tuple(shown), not_shown, missing\n",
                "    return tuple(shown), not_shown, False\n",
            )
        ],
        id="missing-flag-never-returned",
    ),
    pytest.param(
        [("            missing = True\n", "            missing = False\n")],
        id="missing-never-set",
    ),
    pytest.param(
        [("shown = ordered[:MAX_PROVIDER_NOTICES_SHOWN]", "shown = ordered")],
        id="no-cap",
    ),
    pytest.param(
        [
            (
                "shown = ordered[:MAX_PROVIDER_NOTICES_SHOWN]",
                "shown = ordered[:MAX_PROVIDER_NOTICES_SHOWN + 1]",
            )
        ],
        id="cap-off-by-one",
    ),
    pytest.param(
        [("MAX_PROVIDER_NOTICES_SHOWN = 4", "MAX_PROVIDER_NOTICES_SHOWN = 5")],
        id="cap-constant-changed",
    ),
    pytest.param(
        [
            (
                "    not_shown = max(0, len(ordered) - MAX_PROVIDER_NOTICES_SHOWN)\n",
                "    not_shown = 0\n",
            )
        ],
        id="count-not-reported",
    ),
    pytest.param(
        [("            provider_notices_not_shown=not_shown,\n", "")],
        id="count-not-set",
    ),
    pytest.param(
        [
            (
                "            provider_notice_note=PROVIDER_NOTICE_NOTE"
                ' if notices or notice_missing else "",\n',
                '            provider_notice_note="",\n',
            )
        ],
        id="note-missing",
    ),
    pytest.param(
        [
            (
                "            provider_notice_note=PROVIDER_NOTICE_NOTE"
                ' if notices or notice_missing else "",\n',
                "            provider_notice_note=PROVIDER_NOTICE_NOTE,\n",
            )
        ],
        id="note-on-a-view-without-notice",
    ),
    pytest.param(
        [("if notices or notice_missing else", "if notices else")],
        id="note-not-set-for-the-flag-alone",
    ),
    pytest.param(
        [("            provider_notice_missing=notice_missing,\n", "")],
        id="missing-flag-not-filled",
    ),
    pytest.param(
        [
            (
                "            missing = True\n",
                "            missing = True\n"
                '            found.add("provider notice missing for a tides.today item")\n',
            )
        ],
        id="missing-sentence-put-in-the-text-list",
    ),
    pytest.param(
        [("DiveSafe has not checked it.", "DiveSafe has checked it.")],
        id="note-reworded",
    ),
    pytest.param(
        [
            (
                '                "model_host_withheld": True,\n',
                '                "model_host_withheld": True,\n'
                '                "provider_notices": (),\n',
            )
        ],
        id="withheld-view-drops-the-notices",
    ),
    pytest.param(
        [
            (
                "            found.add(notice.strip()[:_MAX_PROVIDER_NOTICE_CHARS])\n",
                "            found.add(notice.strip()[:_MAX_PROVIDER_NOTICE_CHARS])\n"
                '            logger.warning("notice", extra={"notice": notice})\n',
            )
        ],
        id="notice-logged-via-extra",
    ),
    pytest.param(
        [
            (
                "            found.add(notice.strip()[:_MAX_PROVIDER_NOTICE_CHARS])\n",
                "            found.add(notice.strip()[:_MAX_PROVIDER_NOTICE_CHARS])\n"
                '            logger.warning(f"notice {notice}")\n',
            )
        ],
        id="notice-logged-in-message",
    ),
    pytest.param(
        [
            (
                "            record=AssessmentRecordView.of(record),\n"
                "            outcome_note=outcome_note,",
                "            record=AssessmentRecordView.of(record),\n"
                '            outcome_note=outcome_note + " ".join(notices),',
            )
        ],
        id="notice-leaks-into-the-outcome-note",
    ),
]


def test_the_unmutated_view_has_no_misses() -> None:
    assert full_notice_misses(AssessmentView) == []


@pytest.mark.parametrize("edits", VIEW_MUTANTS)
def test_every_mutation_of_the_notice_view_is_caught(edits: list[tuple[str, str]]) -> None:
    try:
        mutant = mutated_module(schemas_module, *edits)
    except (AssertionError, SyntaxError) as error:  # a stale edit target is a failure, not a pass
        pytest.fail(f"the mutation does not apply: {error}")
    assert full_notice_misses(mutant.AssessmentView) != []


def test_a_mutant_that_must_crash_raises_the_specific_exception() -> None:
    """Only a mutant that cannot even build a view may crash, and then with this exact error."""
    mutant = mutated_module(
        schemas_module,
        ("    return tuple(shown), not_shown, missing\n", "    return tuple(shown), not_shown\n"),
    )
    with pytest.raises(ValueError, match="not enough values to unpack"):
        full_notice_misses(mutant.AssessmentView)


def test_a_non_string_check_removed_crashes_with_the_specific_error() -> None:
    """Without the isinstance check a stored `None` reaches `.strip()`: the view raises."""
    mutant = mutated_module(schemas_module, ("isinstance(notice, str) and ", ""))
    record = _record()
    items = tuple(
        e.model_copy(update={"value": {**e.value, "provider_notice": None}})
        for e in record.evidence
    )
    with pytest.raises(AttributeError, match="strip"):
        mutant.AssessmentView.of(record.model_copy(update={"evidence": items}))
