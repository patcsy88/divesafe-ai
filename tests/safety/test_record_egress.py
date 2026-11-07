"""ADR 0022 decision 15 / condition 7: the record's egress fields and the invariant "knowledge in
a prompt implies `declared_local`", checked from the record alone by `AssessmentRecord`.

Knowledge here is evidence with a `kb:` id (owner decision 2026-11-06). Every evidence item below is
SYNTHETIC test data.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from tests.conftest import NOW, make_evidence
from tests.safety.kb_fakes import mutated_module

from divesafe.domain import (
    AssessmentRecord,
    DataCategory,
    DataKind,
    DivePlan,
    EvidenceItem,
    Finding,
    KnowledgeRef,
    Recommendation,
    RecordEgressBasis,
    RuleResult,
    Scenario,
    ScenarioKind,
    StageName,
    StageOutcome,
    StageTrace,
    knowledge_ref,
)
from divesafe.domain import models as models_module
from divesafe.models.egress import EgressBasis

pytestmark = pytest.mark.safety

R = Recommendation
B = RecordEgressBasis
KB_ID = "kb:site-guide:2:7"
REF = KnowledgeRef(document_id="site-guide", version=2, chunk=7)
NOT_DECLARED_LOCAL = [b for b in B if b is not B.DECLARED_LOCAL]
HOST = {
    B.DECLARED_LOCAL: "127.0.0.1",
    B.LOOPBACK_EXTERNAL_OPT_IN: "localhost",
    B.REMOTE_EXTERNAL_OPT_IN: "models.example.org",
    B.NONE: None,
    B.NO_AGENT_STAGE: None,
}


def _knowledge(evidence_id: str = KB_ID) -> EvidenceItem:
    """A SYNTHETIC knowledge item in the record form (no text)."""
    return EvidenceItem(
        id=evidence_id,
        category=DataCategory.SITE_INFORMATION,
        source="kb:site-guide",
        retrieved_at=NOW,
        valid_at=NOW,
        is_forecast=False,
        data_kind=DataKind.KNOWLEDGE,
        value={},
    )


def _doc(*knowledge: EvidenceItem, **fields: Any) -> dict[str, Any]:
    wave = make_evidence(DataCategory.WAVES_SWELL)
    rule = RuleResult(
        rule_id="r", outcome=R.INSUFFICIENT_EVIDENCE, rationale="x", evidence_ids=(wave.id,)
    )
    record = AssessmentRecord(
        id="r1",
        created_at=NOW,
        plan=DivePlan(
            site_id="s",
            planned_start=NOW + timedelta(hours=1),
            planned_duration_minutes=30,
            max_depth_m=10,
        ),
        evidence=(wave,),
        rule_results=(rule,),
        deterministic_recommendation=R.INSUFFICIENT_EVIDENCE,
        proposed_recommendation=None,
        final_recommendation=R.INSUFFICIENT_EVIDENCE,
        llm_attempted_downgrade=False,
        ruleset_version="test",
    )
    doc = record.model_dump(mode="json")
    doc["evidence"] += [k.model_dump(mode="json") for k in knowledge]
    return {**doc, **fields}


def _refused(doc: dict[str, Any], message: str, model: Any = AssessmentRecord) -> bool:
    try:
        model.model_validate(doc)
    except ValidationError as exc:
        return message in str(exc)
    return False


def _held(*refs: KnowledgeRef) -> list[dict[str, Any]]:
    return [r.model_dump() for r in refs]


# --- the enum -----------------------------------------------------------------------------------


def test_the_record_enum_is_the_gate_values_plus_no_agent_stage() -> None:
    gate = set(EgressBasis.__args__)  # type: ignore[attr-defined]
    assert {b.value for b in B} == gate | {"no_agent_stage"}
    assert "no_agent_stage" not in gate


@pytest.mark.parametrize("bad", ["local", "DECLARED_LOCAL", "", 1, True])
def test_an_unrecognised_basis_fails_validation(bad: object) -> None:
    with pytest.raises(ValidationError):
        AssessmentRecord.model_validate(_doc(egress_basis=bad))


def test_a_record_made_before_the_fields_existed_still_validates() -> None:
    doc = _doc()
    for key in ("egress_basis", "model_host", "withheld_knowledge"):
        del doc[key]
    record = AssessmentRecord.model_validate(doc)
    assert (record.egress_basis, record.model_host, record.withheld_knowledge) == (None, None, ())


# --- the invariant ------------------------------------------------------------------------------


@pytest.mark.parametrize("basis", NOT_DECLARED_LOCAL, ids=str)
def test_knowledge_not_withheld_is_refused_unless_declared_local(basis: B) -> None:
    doc = _doc(_knowledge(), egress_basis=basis, model_host=HOST[basis])
    assert _refused(doc, "only under egress basis declared_local")
    held = {**doc, "withheld_knowledge": _held(REF)}
    assert AssessmentRecord.model_validate(held).withheld_knowledge == (REF,)


def test_knowledge_evidence_needs_a_basis_even_when_withheld() -> None:
    """ADR 0022 condition 7: absent is valid only for records with no knowledge evidence."""
    assert _refused(_doc(_knowledge()), "needs an egress basis")
    assert _refused(_doc(_knowledge(), withheld_knowledge=_held(REF)), "needs an egress basis")
    AssessmentRecord.model_validate(_doc(_knowledge("syn-site")))  # not a kb: id


def test_declared_local_may_send_knowledge_or_withhold_it() -> None:
    doc = _doc(_knowledge(), egress_basis="declared_local", model_host="127.0.0.1")
    AssessmentRecord.model_validate(doc)
    AssessmentRecord.model_validate({**doc, "withheld_knowledge": _held(REF)})


@pytest.mark.parametrize("basis", NOT_DECLARED_LOCAL, ids=str)
def test_one_unwithheld_item_among_several_is_refused(basis: B) -> None:
    other = KnowledgeRef(document_id="site-guide", version=2, chunk=8)
    doc = _doc(
        _knowledge(),
        _knowledge("kb:site-guide:2:8"),
        egress_basis=basis,
        model_host=HOST[basis],
        withheld_knowledge=_held(REF),
    )
    assert _refused(doc, "only under egress basis declared_local")
    AssessmentRecord.model_validate({**doc, "withheld_knowledge": _held(REF, other)})


def test_a_withheld_entry_must_name_knowledge_evidence_in_the_record() -> None:
    doc = _doc(egress_basis="none", withheld_knowledge=_held(REF))
    assert _refused(doc, "withheld knowledge must name knowledge evidence")
    other_version = KnowledgeRef(document_id="site-guide", version=3, chunk=7)
    doc = _doc(_knowledge(), egress_basis="none", withheld_knowledge=_held(REF, other_version))
    assert _refused(doc, "withheld knowledge must name knowledge evidence")


def test_a_withheld_entry_appears_once() -> None:
    doc = _doc(_knowledge(), egress_basis="none", withheld_knowledge=_held(REF, REF))
    assert _refused(doc, "appears at most once")


def _citing(channel: str) -> dict[str, Any]:
    finding = Finding(agent="site_intelligence", summary="synthetic", evidence_ids=(KB_ID,))
    if channel == "findings":
        return {"findings": [finding.model_dump(mode="json")]}
    if channel == "cross_examination_findings":
        return {"cross_examination_findings": [finding.model_dump(mode="json")]}
    if channel == "scenarios":
        scenario = Scenario(
            kind=ScenarioKind.MARGINAL, summary="synthetic", evidence_ids=(KB_ID,), confidence=0.5
        )
        return {"scenarios": [scenario.model_dump(mode="json")]}
    if channel == "proposal":
        return {
            "proposal_evidence_ids": [KB_ID],
            "proposed_recommendation": "NO-GO",
            "final_recommendation": "NO-GO",
        }
    reason = StageTrace(
        name=StageName.REASON,
        outcome=StageOutcome.COMPLETED,
        next_stage=None,
        started_at=NOW,
        finished_at=NOW,
        evidence_ids=(KB_ID,),
    )
    return {"stages": [reason.model_dump(mode="json")]}


@pytest.mark.parametrize(
    "channel", ["findings", "cross_examination_findings", "scenarios", "proposal", "reason_trace"]
)
def test_withheld_knowledge_cannot_be_cited_by_model_output(channel: str) -> None:
    doc = _doc(
        _knowledge(),
        egress_basis="remote_external_opt_in",
        model_host="models.example.org",
        withheld_knowledge=_held(REF),
        **_citing(channel),
    )
    assert _refused(doc, "cannot be cited by model output")
    sent = {**doc, "egress_basis": "declared_local", "model_host": "127.0.0.1"}
    sent["withheld_knowledge"] = []
    AssessmentRecord.model_validate(sent)


def test_items_without_a_kb_id_are_not_judged() -> None:
    """The owner's rule (2026-11-06): a KNOWLEDGE item with no `kb:` id is not covered (ADR 0022
    "Not solved"). Invert this test when decision 5's connector-stage KNOWLEDGE filter lands."""
    item = _knowledge("syn-site")
    record = AssessmentRecord.model_validate(_doc(item, egress_basis="none"))
    assert record.withheld_knowledge == ()


@pytest.mark.parametrize(
    "bad",
    [
        "kb:Site-Guide:2:7",
        "kb:site-guide:0:7",
        "kb:site-guide:2:07",
        "kb:site-guide:2:1000000",
        "kb:a-b-c-d:1:1",
        "kb:site-guide:2",
        "kb:site-guide:2:7:1",
        "kb::2:7",
        "KB:site-guide:2:7",
        "Kb:x:1:1",
        "kb:" + "a" * 41 + ":1:1",
    ],
)
def test_a_reserved_id_outside_the_grammar_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        knowledge_ref(bad)
    assert _refused(_doc(_knowledge(bad), egress_basis="none"), "kb:<document_id>")


def test_knowledge_ref_parses_the_three_parts() -> None:
    assert knowledge_ref(KB_ID) == REF
    assert knowledge_ref("kb:" + "a" * 40 + ":999999:1") is not None
    assert knowledge_ref("ev-waves_swell") is None


# --- model_host ---------------------------------------------------------------------------------


@pytest.mark.parametrize("basis", [B.NONE, B.NO_AGENT_STAGE, None], ids=str)
def test_no_host_without_a_model_server(basis: B | None) -> None:
    assert _refused(_doc(egress_basis=basis, model_host="localhost"), "model_host requires")
    AssessmentRecord.model_validate(_doc(egress_basis=basis))


@pytest.mark.parametrize(
    "basis", [B.DECLARED_LOCAL, B.LOOPBACK_EXTERNAL_OPT_IN, B.REMOTE_EXTERNAL_OPT_IN], ids=str
)
def test_a_model_server_basis_needs_its_host(basis: B) -> None:
    assert _refused(_doc(egress_basis=basis), "host name only")
    AssessmentRecord.model_validate(_doc(egress_basis=basis, model_host=HOST[basis]))


@pytest.mark.parametrize(
    "host",
    [
        "https://models.example.org",
        "models.example.org:443",
        "models.example.org/v1",
        "user@models.example.org",
        "Models.Example.org",
        "models.example.org.",
        "[::1]",
        "::1%eth0",
        "fe80::1%x@evil.example:443",
        "1.0x7f",
        "0x7f000001",
        ("a" * 64) + ".example.org",
        "10.0.0.256",
        "0x7f.1",
        "",
        "a b",
        "a" * 254,
    ],
)
def test_model_host_is_a_host_name_only(host: str) -> None:
    doc = _doc(egress_basis="remote_external_opt_in", model_host=host)
    with pytest.raises(ValidationError):
        AssessmentRecord.model_validate(doc)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.8.9.10", "::1", "::ffff:7f00:1"])
@pytest.mark.parametrize("basis", ["declared_local", "loopback_external_opt_in"])
def test_a_loopback_basis_needs_a_loopback_host(basis: str, host: str) -> None:
    AssessmentRecord.model_validate(_doc(egress_basis=basis, model_host=host))
    assert _refused(_doc(egress_basis="remote_external_opt_in", model_host=host), "does not match")


@pytest.mark.parametrize(
    "host", ["models.example.org", "10.0.0.1", "2001:db8::1", "localhost.example"]
)
@pytest.mark.parametrize("basis", ["declared_local", "loopback_external_opt_in"])
def test_a_remote_host_under_a_loopback_basis_is_refused(basis: str, host: str) -> None:
    """ADR 0023: check_egress gives these two bases only when the server's address is loopback.
    A remote host labelled declared_local would let knowledge leave the machine."""
    assert _refused(_doc(egress_basis=basis, model_host=host), "does not match")
    AssessmentRecord.model_validate(_doc(egress_basis="remote_external_opt_in", model_host=host))


def test_more_than_the_bound_of_knowledge_items_is_refused_explicitly() -> None:
    items = [_knowledge(f"kb:site-guide:1:{n}") for n in range(1, 22)]
    assert _refused(_doc(*items, egress_basis="declared_local", model_host="::1"), "at most 20")
    AssessmentRecord.model_validate(
        _doc(*items[:20], egress_basis="declared_local", model_host="::1")
    )


def test_validation_errors_do_not_carry_record_content() -> None:
    doc = _doc(_knowledge(), explanation="private-marker-text")
    with pytest.raises(ValidationError) as caught:
        AssessmentRecord.model_validate(doc)
    assert "private-marker-text" not in str(caught.value)


# --- mutations ----------------------------------------------------------------------------------


def _unwithheld(basis: str, host: str | None) -> dict[str, Any]:
    return _doc(_knowledge(), egress_basis=basis, model_host=host)


@pytest.mark.parametrize(
    ("edit", "doc", "message"),
    [
        (
            ("        self._check_egress()\n", ""),
            _unwithheld("remote_external_opt_in", "models.example.org"),
            "only under egress basis declared_local",
        ),
        (
            (
                "if basis is not RecordEgressBasis.DECLARED_LOCAL and",
                "if basis not in (RecordEgressBasis.DECLARED_LOCAL, RecordEgressBasis.NONE) and",
            ),
            _unwithheld("none", None),
            "only under egress basis declared_local",
        ),
        (
            (
                "if basis is not RecordEgressBasis.DECLARED_LOCAL and",
                "if basis not in (RecordEgressBasis.DECLARED_LOCAL, "
                "RecordEgressBasis.NO_AGENT_STAGE) and",
            ),
            _unwithheld("no_agent_stage", None),
            "only under egress basis declared_local",
        ),
        (
            (
                "if basis is not RecordEgressBasis.DECLARED_LOCAL and",
                "if basis not in (RecordEgressBasis.DECLARED_LOCAL, "
                "RecordEgressBasis.LOOPBACK_EXTERNAL_OPT_IN) and",
            ),
            _unwithheld("loopback_external_opt_in", "localhost"),
            "only under egress basis declared_local",
        ),
        (
            ("        if knowledge and basis is None:\n", "        if False:\n"),
            _doc(_knowledge(), withheld_knowledge=_held(REF)),
            "needs an egress basis",
        ),
        (
            ("        if set(withheld) - knowledge.keys():\n", "        if False:\n"),
            _doc(egress_basis="none", withheld_knowledge=_held(REF)),
            "withheld knowledge must name knowledge evidence",
        ),
        (
            ("        if held_ids & model_cited:\n", "        if False:\n"),
            {
                **_doc(_knowledge(), egress_basis="none", withheld_knowledge=_held(REF)),
                "proposal_evidence_ids": [KB_ID],
                "proposed_recommendation": "NO-GO",
                "final_recommendation": "NO-GO",
            },
            "cannot be cited by model output",
        ),
        (
            ('        or parts[0] != "kb"\n', ""),
            _doc(
                _knowledge("KB:site-guide:2:7"),
                egress_basis="declared_local",
                model_host="127.0.0.1",
            ),
            "kb:<document_id>",
        ),
        (
            ("        return host == host.lower()\n", "        return True\n"),
            _doc(egress_basis="loopback_external_opt_in", model_host="::FFFF:7F00:1"),
            "host name only",
        ),
        (
            ('if len(host) > 253 or "%" in host:', "if len(host) > 253:"),
            _doc(egress_basis="declared_local", model_host="::1%eth0"),
            "host name only",
        ),
        (
            ('label.startswith("0x") or ', ""),
            _doc(egress_basis="remote_external_opt_in", model_host="1.0x7f"),
            "host name only",
        ),
        (
            (
                "        elif (basis in _LOOPBACK_BASES) != _is_loopback(host):\n",
                "        elif False:\n",
            ),
            _doc(egress_basis="declared_local", model_host="models.example.org"),
            "does not match",
        ),
        (
            ("        return address.ipv4_mapped.is_loopback\n", "        return False\n"),
            _doc(egress_basis="remote_external_opt_in", model_host="::ffff:7f00:1"),
            "does not match",
        ),
        (
            ("        if len(knowledge) > MAX_KNOWLEDGE_ITEMS:\n", "        if False:\n"),
            _doc(
                *[_knowledge(f"kb:site-guide:1:{n}") for n in range(1, 22)],
                egress_basis="declared_local",
                model_host="::1",
            ),
            "at most 20",
        ),
        (
            ("        if len(withheld) != len(set(withheld)):\n", "        if False:\n"),
            _doc(_knowledge(), egress_basis="none", withheld_knowledge=_held(REF, REF)),
            "appears at most once",
        ),
        (
            ("            if host is not None:\n", "            if False:\n"),
            _doc(egress_basis="no_agent_stage", model_host="localhost"),
            "model_host requires",
        ),
    ],
    ids=[
        "validator-dropped",
        "none-sends",
        "no-agent-stage-sends",
        "loopback-opt-in-sends",
        "absent-basis",
        "phantom-withheld",
        "withheld-cited",
        "uppercase-prefix",
        "host-case",
        "zone-id",
        "hex-label",
        "basis-host-mismatch",
        "mapped-loopback",
        "knowledge-bound",
        "duplicate-withheld",
        "host-without-server",
    ],
)
def test_mutation(edit: tuple[str, str], doc: dict[str, Any], message: str) -> None:
    mutant = mutated_module(models_module, edit)
    mutant.AssessmentRecord.model_validate(doc)  # the weakened copy accepts it
    assert _refused(doc, message)


def test_the_api_record_view_carries_the_egress_fields() -> None:
    from divesafe.api.schemas import AssessmentView

    record = AssessmentRecord.model_validate(
        _doc(
            _knowledge(),
            egress_basis="remote_external_opt_in",
            model_host="models.example.org",
            withheld_knowledge=_held(REF),
        )
    )
    shown = AssessmentView.of(record).model_dump(mode="json")["record"]
    assert shown["egress_basis"] == "remote_external_opt_in"
    assert shown["model_host"] == "models.example.org"
    assert shown["withheld_knowledge"] == [{"document_id": "site-guide", "version": 2, "chunk": 7}]
