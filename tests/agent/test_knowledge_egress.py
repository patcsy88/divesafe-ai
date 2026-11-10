"""ADR 0022 decision 15: the egress filter. Knowledge reaches a prompt only under the exact basis
`declared_local`; anything else (another basis, `no_agent_stage`, None, an unknown value, a
non-string) withholds every knowledge item, lists it in `withheld_knowledge` and keeps its id out of
`ALLOWED_EVIDENCE_IDS`. Supervise checks citations against the ids actually sent. `build_provider`
returns `(provider, basis)`; a create-only key is not shown `model_host`. Mutation-checked."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError
from tests.agent import scripted_llm as sl
from tests.agent.test_knowledge_connector import NOW, SITE, chunk
from tests.api_polling import create_and_poll
from tests.safety.kb_fakes import mutated_module
from tests.safety.test_api_human_gate import AUTH_A, PLAN, _build, _hash

from divesafe.agents import run_agent_stage, split_for_egress
from divesafe.agents import stage as stage_module
from divesafe.agents.prompts import ABSENCE_LINE, WITHHELD_FINDING_SUMMARY, WITHHELD_KNOWLEDGE_NOTE
from divesafe.agents.specialists import SITE_INTELLIGENCE
from divesafe.api import schemas as schemas_module
from divesafe.api import state as state_module
from divesafe.api.auth import ApiKeyAuthenticator, KeyEntry, Role
from divesafe.api.schemas import AssessmentView
from divesafe.config import Settings
from divesafe.data import DataGovMyWarningConnector, FetchResult, OpenMeteoMarineConnector
from divesafe.domain import (
    AssessmentRecord,
    EvidenceItem,
    KnowledgeRef,
    Recommendation,
    RecordEgressBasis,
    SupervisorCheck,
)
from divesafe.models import FakeProvider
from divesafe.models.egress import EgressBasis, check_egress
from divesafe.orchestration import pipeline as pipeline_module
from divesafe.rag.items import run_item

pytestmark = pytest.mark.safety

R = Recommendation
SITE_DOC = "kb:site-doc:1:1"
GUIDE = "kb:guide:1:1"
REFS = (
    KnowledgeRef(document_id="guide", version=1, chunk=1),
    KnowledgeRef(document_id="site-doc", version=1, chunk=1),
)
UNLISTED = "manual:1"  # KNOWLEDGE kind without a `kb:` id: withheld, not in withheld_knowledge
WITHHOLDING: list[Any] = [
    "loopback_external_opt_in",
    "remote_external_opt_in",
    "none",
    "no_agent_stage",
    None,
    "Declared_Local",
    "declared_local ",
    "unknown",
    1,
    RecordEgressBasis.LOOPBACK_EXTERNAL_OPT_IN,
]
HOST = {"loopback_external_opt_in": "localhost", "remote_external_opt_in": "llm.example.org"}


def _kb_items() -> tuple[EvidenceItem, ...]:
    return (
        run_item(chunk("site-doc", site=SITE.id), SITE, NOW),
        run_item(chunk("guide"), SITE, NOW),
    )


def _unlisted() -> EvidenceItem:
    return _kb_items()[0].model_copy(update={"id": UNLISTED})


class _Knowledge:
    """Stand-in for Retrieve (not wired yet): a connector that returns knowledge items."""

    name = "kb-stub"

    def __init__(self, unlisted: bool) -> None:
        self.unlisted = unlisted

    async def fetch(self, *_args: object) -> FetchResult:
        return FetchResult(items=(*_kb_items(), *((_unlisted(),) if self.unlisted else ())))


def _connectors(unlisted: bool = False) -> list[Any]:
    return [
        OpenMeteoMarineConnector(sl._Getter(sl.MARINE)),
        DataGovMyWarningConnector(sl._Getter(sl.WARNINGS)),
        _Knowledge(unlisted),
    ]


def _run(
    provider: Any, basis: Any, host: str | None, module: Any = None, unlisted: bool = False
) -> AssessmentRecord:
    if module is None:
        return sl.run_pipeline(
            provider, connectors=_connectors(unlisted), egress_basis=basis, model_host=host
        )
    record: AssessmentRecord = asyncio.run(
        module.assess_dive(
            assessment_id="agent-1",
            plan=sl.PLAN,
            site=SITE,
            connectors=_connectors(unlisted),
            engine=sl.engine(with_warning_rule=True),
            now=sl.NOW,
            provider=provider,
            clock=lambda: sl.NOW,
            egress_basis=basis,
            model_host=host,
        )
    )
    return record


def _texts(provider: FakeProvider) -> str:
    return "\n".join(str(r.messages[1].content) for r in provider.requests)


def _allowed(provider: FakeProvider) -> set[str]:
    return {i for r in provider.requests for i in sl.allowed_ids(r)}


# --- the split -----------------------------------------------------------------------------------


def test_declared_local_sends_every_knowledge_item() -> None:
    split = split_for_egress(_kb_items(), "declared_local")
    assert [e.id for e in split.sent] == [SITE_DOC, GUIDE]
    assert (split.withheld, split.any_withheld) == ((), False)


@pytest.mark.parametrize("basis", WITHHOLDING, ids=repr)
def test_every_other_basis_withholds_all_knowledge(basis: Any) -> None:
    split = split_for_egress(_kb_items(), basis)
    assert split.sent == () and split.any_withheld
    assert set(split.withheld) == set(REFS)


def test_non_knowledge_is_always_sent() -> None:
    weather = _kb_items()[0].model_copy(update={"id": "x:1", "data_kind": "observation"})
    assert split_for_egress([weather], None).sent == (weather,)


def test_a_knowledge_kind_without_a_kb_id_is_withheld_but_not_listed() -> None:
    item = _kb_items()[0].model_copy(update={"id": "manual:1"})
    split = split_for_egress([item], "remote_external_opt_in")
    assert (split.sent, split.withheld, split.any_withheld) == ((), (), True)


@pytest.mark.parametrize("bad", ["kb:Bad_Doc:1:1", "kb:doc:0:1", "KB:doc:1:1"])
def test_a_malformed_kb_id_is_never_sent_even_under_declared_local(bad: str) -> None:
    item = _kb_items()[0].model_copy(update={"id": bad})
    split = split_for_egress([item], "declared_local")
    assert (split.sent, split.withheld, split.any_withheld) == ((), (), True)


def test_a_duplicate_knowledge_ref_is_listed_once() -> None:
    item = _kb_items()[0]
    assert split_for_egress([item, item], "none").withheld == (REFS[1],)


# --- the stage: every prompt ---------------------------------------------------------------------


def test_run_agent_stage_has_no_default_basis() -> None:
    param = inspect.signature(run_agent_stage).parameters["egress_basis"]
    assert param.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        asyncio.run(run_agent_stage(sl.fake(), sl.PLAN, [], [], R.INSUFFICIENT_EVIDENCE))  # type: ignore[call-arg]


def test_assess_dive_has_no_default_basis_or_host() -> None:
    params = inspect.signature(pipeline_module.assess_dive).parameters
    for name in ("egress_basis", "model_host"):
        assert params[name].default is inspect.Parameter.empty, name
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name


def _stage(provider: FakeProvider, basis: Any, extra: tuple[EvidenceItem, ...] = ()) -> Any:
    return asyncio.run(
        run_agent_stage(
            provider,
            sl.PLAN,
            (*_kb_items(), *extra),
            [],
            R.INSUFFICIENT_EVIDENCE,
            egress_basis=basis,
        )
    )


def test_declared_local_stage_offers_knowledge_to_the_models() -> None:
    provider = sl.fake()
    result = _stage(provider, "declared_local")
    assert {SITE_DOC, GUIDE} <= _allowed(provider)
    assert result.withheld_knowledge == ()
    assert set(result.sent_evidence_ids) == {SITE_DOC, GUIDE}


@pytest.mark.parametrize("basis", WITHHOLDING, ids=repr)
def test_a_withholding_basis_keeps_knowledge_out_of_every_prompt(basis: Any) -> None:
    provider = sl.fake()
    result = _stage(provider, basis)
    assert not _allowed(provider) & {SITE_DOC, GUIDE}
    assert "site-doc" not in _texts(provider) and "Text of guide" not in _texts(provider)
    assert set(result.withheld_knowledge) == set(REFS)
    assert result.sent_evidence_ids == ()
    site = next(f for f in result.findings if f.agent == SITE_INTELLIGENCE)
    assert (site.summary, site.no_evidence) == (WITHHELD_FINDING_SUMMARY, True)


def test_the_withheld_note_replaces_the_absence_line_when_knowledge_was_held_back() -> None:
    provider = sl.fake()
    live = tuple(e for e in sl.run_pipeline(sl.fake()).evidence if not e.id.startswith("kb:"))
    _stage(provider, "none", live)  # live items, so the models are called
    text = _texts(provider)
    assert text
    assert WITHHELD_KNOWLEDGE_NOTE.strip() in text and ABSENCE_LINE.strip() not in text


STAGE_MUTATIONS = [
    pytest.param(("    evidence = split.sent\n", ""), id="filter-not-applied"),
    pytest.param(
        (
            "    return isinstance(basis, str) and basis == ",
            "    return basis is not None or basis == ",
        ),
        id="any-basis-is-local",
    ),
    pytest.param(
        ("        if local or (ref is None and not is_knowledge(item)):\n",
         "        if local or ref is None:\n"),
        id="knowledge-kind-ignored",
    ),
    pytest.param(
        ("        sent_evidence_ids=tuple(e.id for e in evidence),\n",
         "        sent_evidence_ids=tuple(e.id for e in split.sent) + ('kb:guide:1:1',),\n"),
        id="sent-ids-include-withheld",
    ),
    pytest.param(
        ("        withheld_knowledge=split.withheld,\n", ""),
        id="withheld-not-reported",
    ),
]  # fmt: skip


@pytest.mark.parametrize("edit", STAGE_MUTATIONS)
def test_mutation_of_the_stage_filter_is_caught(edit: tuple[str, str]) -> None:
    mutant = mutated_module(stage_module, edit)
    provider = sl.fake()
    knowledge_kind = _kb_items()[0].model_copy(update={"id": "manual:1"})
    result = asyncio.run(
        mutant.run_agent_stage(
            provider,
            sl.PLAN,
            (*_kb_items(), knowledge_kind),
            [],
            R.INSUFFICIENT_EVIDENCE,
            egress_basis="remote_external_opt_in",
        )
    )
    leaked = _allowed(provider) & {SITE_DOC, GUIDE, "manual:1"}
    assert (
        leaked
        or set(result.withheld_knowledge) != set(REFS)
        or set(result.sent_evidence_ids) & {SITE_DOC, GUIDE, "manual:1"}
    )


# --- the pipeline and the stored record ----------------------------------------------------------


def test_declared_local_record_keeps_knowledge_and_lists_nothing() -> None:
    provider = sl.fake()
    record = _run(provider, "declared_local", "localhost")
    assert (record.egress_basis, record.model_host) == (
        RecordEgressBasis.DECLARED_LOCAL,
        "localhost",
    )
    assert record.withheld_knowledge == ()
    assert {SITE_DOC, GUIDE} <= _allowed(provider)


@pytest.mark.parametrize(
    ("basis", "host", "stored", "stored_host"),
    [
        ("loopback_external_opt_in", "localhost", "loopback_external_opt_in", "localhost"),
        ("remote_external_opt_in", "llm.example.org", "remote_external_opt_in", "llm.example.org"),
        ("none", "localhost", "none", None),
        ("no_agent_stage", None, "none", None),
        (None, "localhost", "none", None),
        ("unknown", "localhost", "none", None),
        (7, "localhost", "none", None),
        ("declared_local", None, "none", None),
    ],
)
def test_a_withholding_basis_is_stored_with_every_knowledge_item_listed(
    basis: Any, host: str | None, stored: str, stored_host: str | None
) -> None:
    provider = sl.fake()
    record = _run(provider, basis, host)
    assert record.egress_basis == RecordEgressBasis(stored)
    assert record.model_host == stored_host
    assert set(record.withheld_knowledge) == set(REFS)
    assert {SITE_DOC, GUIDE} <= {e.id for e in record.evidence}
    assert not _allowed(provider) & {SITE_DOC, GUIDE}
    assert "Text of site-doc" not in _texts(provider)
    # The stored record round-trips through its own validator.
    assert AssessmentRecord.model_validate_json(record.model_dump_json()) == record


def test_no_provider_records_no_agent_stage_whatever_the_basis_says() -> None:
    record = _run(None, "declared_local", "localhost")
    assert (record.egress_basis, record.model_host) == (RecordEgressBasis.NO_AGENT_STAGE, None)
    assert set(record.withheld_knowledge) == set(REFS)


def test_supervise_checks_citations_against_the_ids_actually_sent() -> None:
    """The runner refuses an unknown id first, and the record refuses a cited `kb:` item it lists
    as withheld; Supervise is the line for a withheld KNOWLEDGE-kind item with no `kb:` id. A
    proposal citing it fails `citations_valid`; checking against all evidence would pass it."""
    record = _run(sl.fake(), "none", None, unlisted=True)
    assert UNLISTED in {e.id for e in record.evidence}
    assert record.supervisor_verdict is not None
    assert SupervisorCheck.CITATIONS_VALID in record.supervisor_verdict.passed
    edit = (
        "run.sent_ids if run.sent_ids is not None else (item.id for item in evidence.items)",
        "(item.id for item in evidence.items)",
    )
    real = _supervised_with_a_withheld_citation(pipeline_module)
    broken = _supervised_with_a_withheld_citation(mutated_module(pipeline_module, edit))
    assert SupervisorCheck.CITATIONS_VALID in real
    assert SupervisorCheck.CITATIONS_VALID not in broken


def _supervised_with_a_withheld_citation(module: Any) -> tuple[SupervisorCheck, ...]:
    """The module's pipeline with a stage whose proposal also cites the withheld `manual:1`;
    returns Supervise's failed checks."""
    original = module.run_agent_stage

    async def citing(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        assert UNLISTED not in result.sent_evidence_ids
        return replace(result, proposal_evidence_ids=(*result.proposal_evidence_ids, UNLISTED))

    module.run_agent_stage = citing
    try:
        record = _run(sl.fake(), "none", None, module=module, unlisted=True)
    finally:
        module.run_agent_stage = original
    assert record.supervisor_verdict is not None
    failed: tuple[SupervisorCheck, ...] = record.supervisor_verdict.failed
    return failed


PIPELINE_MUTATIONS = [
    pytest.param(
        ("deterministic, egress_basis=basis\n", "deterministic, egress_basis='declared_local'\n"),
        id="basis-not-passed",
    ),
    pytest.param(
        (
            " - {RecordEgressBasis.NO_AGENT_STAGE.value}\n",
            "\n",
        ),
        id="provider-may-claim-no-agent-stage",
    ),
    pytest.param(
        ("withheld_knowledge=split_for_egress(evidence.items, run.egress_basis.value)",
         "withheld_knowledge=(),  # ("),
        id="withheld-not-recorded",
    ),
    pytest.param(
        ("    if model_host is None:\n        # A basis", "    if False:\n        # A basis"),
        id="hostless-basis-kept",
    ),
]  # fmt: skip


@pytest.mark.parametrize("edit", PIPELINE_MUTATIONS)
def test_mutation_of_the_pipeline_egress_is_caught(edit: tuple[str, str]) -> None:
    """Caught = a leak, a wrong stored basis, host or withheld list, or the record validator
    refusing the mutant's record. Any other exception is a crash, not a catch."""
    mutant = mutated_module(pipeline_module, edit)
    expected = {
        ("remote_external_opt_in", "llm.example.org"): (
            "remote_external_opt_in",
            "llm.example.org",
        ),
        ("no_agent_stage", "localhost"): ("none", None),
        ("declared_local", None): ("none", None),
    }
    caught = []
    for (basis, host), (stored, stored_host) in expected.items():
        provider = sl.fake()
        try:
            record = _run(provider, basis, host, module=mutant)
        except ValidationError:
            caught.append("refused")
            continue
        if (
            _allowed(provider) & {SITE_DOC, GUIDE}
            or set(record.withheld_knowledge) != set(REFS)
            or (record.egress_basis, record.model_host) != (RecordEgressBasis(stored), stored_host)
        ):
            caught.append(repr(basis))
    assert caught


# --- build_provider and model_host ---------------------------------------------------------------


def test_the_fake_setting_gives_the_no_agent_stage_pair_without_check_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_run(*_a: object) -> None:
        raise AssertionError("check_egress was called for the fake provider setting")

    monkeypatch.setattr(state_module, "check_egress", must_not_run)
    assert state_module.build_provider(Settings(_env_file=None)) == (None, "no_agent_stage")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("flags", "basis"),
    [
        ({"llm_server_is_local": True}, "declared_local"),
        ({"allow_external_llm": True}, "loopback_external_opt_in"),
    ],
)
def test_a_real_provider_comes_with_check_egress_basis(flags: dict[str, bool], basis: str) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        llm_provider="openai_compatible",
        llm_model="m",
        llm_base_url="http://127.0.0.1:8000/v1",
        **flags,
    )
    provider, got = state_module.build_provider(settings)
    assert provider is not None and got == basis == check_egress(provider, settings)


def test_check_egress_never_returns_no_agent_stage() -> None:
    assert "no_agent_stage" not in EgressBasis.__args__  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("http://[::1]:8000/v1", "::1"),
        ("http://user:pw@LocalHost:8000/v1", "localhost"),
        ("https://llm.example.org/v1?x=1", "llm.example.org"),
    ],
)
def test_model_host_is_the_host_name_only(url: str, host: str) -> None:
    settings = Settings(_env_file=None, llm_base_url=url)  # type: ignore[call-arg]
    assert state_module.model_host(settings) == host


def test_mutation_of_build_provider_is_caught() -> None:
    mutant = mutated_module(
        state_module,
        ('        return None, "no_agent_stage"', '        return None, "declared_local"'),
    )
    assert mutant.build_provider(Settings(_env_file=None)) != (None, "no_agent_stage")  # type: ignore[call-arg]
    host = mutated_module(
        state_module,
        ("    return urlsplit(str(settings.llm_base_url)).hostname\n",
         "    return urlsplit(str(settings.llm_base_url)).netloc\n"),
    )  # fmt: skip
    url = "http://[::1]:8000/v1"
    assert host.model_host(Settings(_env_file=None, llm_base_url=url)) != "::1"  # type: ignore[call-arg]


# --- the view: model_host hidden from a create-only key ------------------------------------------

CREATE_ONLY = "key-create-only-test"
CREATE_AUTH = {"Authorization": f"Bearer {CREATE_ONLY}"}
BODY = {**PLAN, "decider": "alice"}


def _client() -> Any:
    client, _, repo = _build(provider=sl.fake())
    state = client.app.state.divesafe
    state.egress_basis, state.model_host = "declared_local", "localhost"
    state.authenticator = ApiKeyAuthenticator(
        {
            _hash("key-for-alice-test-only"): KeyEntry("alice", frozenset(Role)),
            _hash(CREATE_ONLY): KeyEntry("carol", frozenset({Role.ASSESSOR})),
        }
    )
    return client, repo


def _hidden(body: dict[str, Any]) -> None:
    assert body["model_host_withheld"] is True
    assert "model_host" not in body["record"]  # left out, never null
    assert body["record"]["egress_basis"] == "declared_local"


def test_a_create_only_key_never_sees_model_host_on_create_poll_or_later_read() -> None:
    client, repo = _client()
    key = {**CREATE_AUTH, "Idempotency-Key": "egress-host-key-01"}
    done = create_and_poll(
        client, BODY, CREATE_AUTH, extra_headers={"Idempotency-Key": key["Idempotency-Key"]}
    )
    _hidden(done.json())
    rid = done.json()["record"]["id"]
    assert repo.get(rid).model_host == "localhost"  # stored, only hidden
    _hidden(client.get(f"/v1/assessments/{rid}", headers=CREATE_AUTH).json())
    replay = client.post("/v1/assessments", json=BODY, headers=key)
    assert replay.status_code == 200
    _hidden(replay.json())
    assert "localhost" not in client.get(f"/v1/assessments/{rid}", headers=CREATE_AUTH).text


def test_other_roles_still_see_model_host() -> None:
    client, _ = _client()
    body = create_and_poll(client, PLAN, AUTH_A).json()
    assert body["model_host_withheld"] is False
    assert body["record"]["model_host"] == "localhost"


def test_a_null_host_is_shown_as_null_to_other_roles_and_never_means_hidden() -> None:
    record = sl.run_pipeline(None)
    shown = AssessmentView.of(record).model_dump(mode="json")
    assert shown["record"]["model_host"] is None and shown["model_host_withheld"] is False


VIEW_MUTATIONS = [
    pytest.param(("        view.record._hide_model_host = True\n", ""), id="host-not-hidden"),
    pytest.param(('                "model_host_withheld": True,\n', ""), id="flag-not-set"),
    pytest.param(
        ('            data.pop("model_host", None)\n', '            data["model_host"] = None\n'),
        id="null-instead-of-absent",
    ),
]


@pytest.mark.parametrize("edit", VIEW_MUTATIONS)
def test_mutation_of_the_host_hiding_is_caught(edit: tuple[str, str]) -> None:
    mutant = mutated_module(schemas_module, edit)
    record = sl.run_pipeline(sl.fake())
    assert record.model_host == "localhost"
    body = mutant.AssessmentView.withheld(record).model_dump(mode="json")
    assert body["model_host_withheld"] is not True or "model_host" in body["record"]
