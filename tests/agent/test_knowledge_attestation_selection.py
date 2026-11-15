"""ADR 0028 decision 7: under the run basis `remote_knowledge_opt_in` a governing version without
the model-host attestation returns nothing from its family, with the fixed issue and no fall-back
to an older attested version. The exclusion is in SQL and again in code, keyed on the run's
recorded basis, never a setting. Bases are literals. Mutations edit COPIES of `rag.selection`,
`rag.connector` and `rag.store`, never `src`. The live SQL half is in
`test_knowledge_connector_live.py` (skipped without a test database)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tests.agent.test_knowledge_connector import (
    DAY,
    END,
    NOW,
    SITE,
    START,
    TODAY,
    FakeStore,
    chunk,
    ver,
)
from tests.safety.kb_fakes import mutated_module

from divesafe.rag import connector as connector_module
from divesafe.rag import selection as selection_module
from divesafe.rag import store as store_module
from divesafe.rag.connector import KnowledgeConnector
from divesafe.rag.store import Selection, parameters

pytestmark = pytest.mark.safety

KNOWLEDGE = "remote_knowledge_opt_in"
REMOTE = "remote_external_opt_in"
NOT_ATTESTED = "knowledge store: a document version is not approved for the model host"
DISAGREEMENT_FRAGMENT = "disagree"


def _sel(versions: list[Any], chunks: list[Any]) -> Selection:
    return Selection(tuple(versions), tuple(chunks), len(chunks))


def _retrieve(store: Any, basis: object, module: Any = connector_module) -> Any:
    return asyncio.run(
        module.KnowledgeConnector(store).retrieve(SITE, START, END, NOW, egress_basis=basis)
    )


def _ids(result: Any) -> list[str]:
    return [i.id for i in result.fetch.items]


def _issues(result: Any) -> tuple[str, ...]:
    return tuple(result.fetch.issues)


def _no_disagreement(result: Any) -> bool:
    return not any(DISAGREEMENT_FRAGMENT in i for i in _issues(result))


# v3 attested and approved earlier; v4 approved since, without the attestation. v4 governs.
V3 = ver("site-guide", 3, attests=True, approved_on=TODAY - 20 * DAY, eff=TODAY - 30 * DAY)
V4 = ver("site-guide", 4, attests=False)
V3_CHUNK = chunk("site-guide", 3)
V4_CHUNK = chunk("site-guide", 4)
# What the real SQL returns for each basis (the store does the SQL half; the live test proves it).
SQL_UNDER_KNOWLEDGE = _sel([V3, V4], [])
SQL_UNDER_REMOTE = _sel([V3, V4], [V4_CHUNK])


def test_an_attested_governing_version_is_returned_under_the_knowledge_basis() -> None:
    attested = ver("site-guide", 4, attests=True)
    result = _retrieve(FakeStore(_sel([V3, attested], [V4_CHUNK])), KNOWLEDGE)
    assert _ids(result) == ["kb:site-guide:4:1"]
    assert _issues(result) == ()


def test_an_unattested_governing_version_returns_nothing_and_never_falls_back() -> None:
    result = _retrieve(FakeStore(SQL_UNDER_KNOWLEDGE), KNOWLEDGE)
    assert _ids(result) == []
    assert _issues(result) == (NOT_ATTESTED,)
    assert _no_disagreement(result)
    assert not any(":3:" in i for i in _ids(result))


def test_an_unattested_family_does_not_hide_an_attested_one() -> None:
    other = ver("guide", 1, attests=True)
    store = FakeStore(_sel([V3, V4, other], [chunk("guide", 1)]))
    result = _retrieve(store, KNOWLEDGE)
    assert _ids(result) == ["kb:guide:1:1"]
    assert _issues(result) == (NOT_ATTESTED,)


def test_the_store_leaking_unattested_chunks_is_caught_in_code() -> None:
    """The code half: even if SQL returned the v4 (or v3) chunks, the code layer sends none."""
    for leaked in ([V4_CHUNK], [V3_CHUNK], [V3_CHUNK, V4_CHUNK]):
        result = _retrieve(FakeStore(_sel([V3, V4], leaked)), KNOWLEDGE)
        assert _ids(result) == [], leaked
        assert NOT_ATTESTED in _issues(result)


@pytest.mark.parametrize(
    "basis",
    [REMOTE, "declared_local", "loopback_external_opt_in", "none", "no_agent_stage", None],
)
def test_other_bases_select_as_before(basis: object) -> None:
    """Unchanged selection: v4 governs and is returned (the egress filter withholds it later)."""
    result = _retrieve(FakeStore(SQL_UNDER_REMOTE), basis)
    assert _ids(result) == ["kb:site-guide:4:1"]
    assert _issues(result) == ()


@pytest.mark.parametrize(
    "basis",
    [
        "REMOTE_KNOWLEDGE_OPT_IN",
        "remote_knowledge_opt_in ",
        b"remote_knowledge_opt_in",
        ["remote_knowledge_opt_in"],
    ],
)
def test_a_near_miss_basis_does_not_exclude(basis: object) -> None:
    """Exclusion is keyed on the exact value. A near miss is not a sending basis either: the
    egress filter withholds every knowledge item for it (`test_knowledge_egress.py`)."""
    assert parameters(SITE.id, NOW, END, egress_basis=basis)["exclude_unattested"] is False


def test_the_record_enum_member_excludes() -> None:
    from divesafe.domain.models import RecordEgressBasis

    basis = RecordEgressBasis.REMOTE_KNOWLEDGE_OPT_IN
    assert parameters(SITE.id, NOW, END, egress_basis=basis)["exclude_unattested"] is True
    result = _retrieve(FakeStore(SQL_UNDER_KNOWLEDGE), basis)
    assert _issues(result) == (NOT_ATTESTED,)


def test_the_sql_parameter_follows_the_basis() -> None:
    assert parameters(SITE.id, NOW, END, egress_basis=KNOWLEDGE) == {
        "site_id": SITE.id,
        "now": NOW,
        "window_end": END,
        "exclude_unattested": True,
    }
    assert parameters(SITE.id, NOW, END, egress_basis=REMOTE)["exclude_unattested"] is False


def test_the_sql_excludes_at_the_chosen_step_not_before_the_governing_one() -> None:
    sql = store_module.SELECTION_SQL
    condition = "AND (NOT %(exclude_unattested)s::boolean OR g.attests_model_host_egress IS TRUE)"
    assert sql.count(condition) == 1
    governing = sql.index("governing AS (")
    chosen = sql.index("chosen AS (")
    assert governing < chosen < sql.index(condition)
    assert sql[governing:chosen].count("attests") == 0


def test_one_connector_serves_both_bases_and_excludes_only_for_the_knowledge_run() -> None:
    store = FakeStore(SQL_UNDER_REMOTE)
    connector = KnowledgeConnector(store)
    remote = asyncio.run(connector.retrieve(SITE, START, END, NOW, egress_basis=REMOTE))
    store.selection = SQL_UNDER_KNOWLEDGE
    knowledge = asyncio.run(connector.retrieve(SITE, START, END, NOW, egress_basis=KNOWLEDGE))
    store.selection = SQL_UNDER_REMOTE
    again = asyncio.run(connector.retrieve(SITE, START, END, NOW, egress_basis=REMOTE))
    assert store.bases == [REMOTE, KNOWLEDGE, REMOTE]
    assert _ids(remote) == _ids(again) == ["kb:site-guide:4:1"]
    assert _ids(knowledge) == [] and _issues(knowledge) == (NOT_ATTESTED,)


def test_fetch_passes_the_knowledge_basis() -> None:
    store = FakeStore(SQL_UNDER_KNOWLEDGE)
    result = asyncio.run(KnowledgeConnector(store).fetch(SITE, START, END, NOW))
    assert store.bases == [KNOWLEDGE]
    assert result.items == () and result.issues == (NOT_ATTESTED,)


# --- mutations ------------------------------------------------------------------------------


def test_mutation_exclusion_in_sql_only_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the code half, SQL's answer (no v4 chunks) disagrees with the code's expectation:
    a disagreement issue appears and the fixed issue goes."""
    mutant = mutated_module(
        selection_module,
        (
            "    if result.kind is not FamilyClass.RETURNED or not excludes_unattested(egress_basis):\n",  # noqa: E501
            "    if True:\n",
        ),
    )
    real = selection_module.classify((V3, V4), NOW, END, egress_basis=KNOWLEDGE)
    mutated = mutant.classify((V3, V4), NOW, END, egress_basis=KNOWLEDGE)
    assert [r.kind.value for r in real] == ["not_attested"]
    assert [r.kind.value for r in mutated] == ["returned"]
    # Through the connector. The mutant's own `FamilyClass` is a copy that never compares equal
    # to the connector's, so the same edit is applied to the real module's `classify_family`
    # (monkeypatched, restored after the test).
    monkeypatch.setattr(
        selection_module,
        "classify_family",
        lambda rows, now, window_end, *, egress_basis: selection_module._classify_family(
            rows, now, window_end
        ),
    )
    result = _retrieve(FakeStore(SQL_UNDER_KNOWLEDGE), KNOWLEDGE)
    assert not _no_disagreement(result) and NOT_ATTESTED not in _issues(result)
    # And the leaking store then leaks v4.
    leak = _retrieve(FakeStore(_sel([V3, V4], [V4_CHUNK])), KNOWLEDGE)
    assert _ids(leak) == ["kb:site-guide:4:1"]


def test_mutation_exclusion_in_code_only_is_caught() -> None:
    """Without the SQL half, the query condition is gone (and the live test sees v4 chunks)."""
    mutant = mutated_module(
        store_module,
        (
            "      AND (NOT %(exclude_unattested)s::boolean OR g.attests_model_host_egress IS TRUE)\n",  # noqa: E501
            "",
        ),
    )
    assert "exclude_unattested" in store_module.SELECTION_SQL
    assert "exclude_unattested" not in mutant.SELECTION_SQL


def test_mutation_exclusion_before_the_governing_version_is_caught() -> None:
    """Filtering the unattested versions out first makes v3 govern: a fall-back."""
    mutant = mutated_module(
        selection_module,
        (
            "    result = _classify_family(rows, now, window_end)\n",
            "    if excludes_unattested(egress_basis):\n"
            "        rows = [r for r in rows if r.attests_model_host_egress] or list(rows)\n"
            "    result = _classify_family(rows, now, window_end)\n",
        ),
    )
    mutated = mutant.classify((V3, V4), NOW, END, egress_basis=KNOWLEDGE)
    assert [(r.kind.value, r.version) for r in mutated] == [("returned", 3)]
    real = selection_module.classify((V3, V4), NOW, END, egress_basis=KNOWLEDGE)
    assert [(r.kind.value, r.version) for r in real] == [("not_attested", None)]


@pytest.mark.parametrize(
    "fixed",
    [KNOWLEDGE, REMOTE],
    ids=["always-excludes", "never-excludes"],
)
def test_mutation_exclusion_keyed_on_a_start_up_value_is_caught(fixed: str) -> None:
    """A connector that uses one value fixed at start-up instead of each run's basis."""
    mutant = mutated_module(
        connector_module,
        (
            "                self._store.select, site.id, now, window_end, egress_basis=egress_basis\n",  # noqa: E501
            f"                self._store.select, site.id, now, window_end, egress_basis={fixed!r}\n",  # noqa: E501
        ),
        (
            "            window_end,\n            egress_basis=egress_basis,\n        )\n        logger",  # noqa: E501
            f"            window_end,\n            egress_basis={fixed!r},\n        )\n        logger",  # noqa: E501
        ),
    )
    store = FakeStore(SQL_UNDER_REMOTE)
    remote = _retrieve(store, REMOTE, module=mutant)
    store.selection = SQL_UNDER_KNOWLEDGE
    knowledge = _retrieve(store, KNOWLEDGE, module=mutant)
    correct = _ids(remote) == ["kb:site-guide:4:1"] and _issues(knowledge) == (NOT_ATTESTED,)
    assert not correct
