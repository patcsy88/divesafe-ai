"""ADR 0028 decision 8: manifest format 2 and its model-host attestation.

Format 1 is refused by the loader (`parse_manifest`, its own fixed reason) and by the builder
(`parse_draft`, status 16). Format 2 without the attestation, or with it anything but `true`, is
refused (cases in `test_kb_manifest.py`). A loaded version stores `true`. Mutations run on COPIES of
the module source, never on `src`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.safety.kb_build_draft import corpus, document, draft, outcome
from tests.safety.kb_corpus import write_corpus
from tests.safety.kb_fakes import LoadFakeConnection, mutated_module
from tests.safety.test_kb_load_flow import Env, go, load_args

from divesafe.services import kb_manifest, kb_manifest_build
from divesafe.services.kb_manifest import ManifestInvalid, parse_manifest
from divesafe.services.kb_manifest_build import BuildRefused, Outcome, parse_draft

pytestmark = pytest.mark.safety

FORMAT_1 = (
    "manifest format 1 is no longer accepted: rebuild the draft as format 2, which attests "
    "that the documents may be sent to the configured model host"
)
GENERIC = "the manifest is not valid"
DRAFT_FORMAT_1 = Outcome(
    16,
    "refused: draft format 1 is no longer accepted; write format 2 with "
    "attests_model_host_egress (ADR 0028)",
)


def _manifest_bytes(root: Path, **top: Any) -> bytes:
    write_corpus(root, top=top or None)
    return (root / "manifest.json").read_bytes()


def _refusal(module: Any, raw: bytes) -> str | None:
    try:
        module.parse_manifest(raw)
    except module.ManifestInvalid as refused:
        return str(refused)
    return None


def test_a_format_2_manifest_is_accepted(tmp_path: Path) -> None:
    manifest, _ = parse_manifest(_manifest_bytes(tmp_path))
    assert manifest.format == 2
    assert manifest.attests_model_host_egress is True


def test_format_1_is_refused_with_its_own_reason(tmp_path: Path) -> None:
    """Even a manifest that is format 1 in every other respect (no attestation field)."""
    raw = json.loads(_manifest_bytes(tmp_path))
    raw["format"] = 1
    del raw["attests_model_host_egress"]
    with pytest.raises(ManifestInvalid) as caught:
        parse_manifest(json.dumps(raw).encode())
    assert str(caught.value) == FORMAT_1
    # And with the attestation present: still format 1, still refused.
    raw["attests_model_host_egress"] = True
    assert _refusal(kb_manifest, json.dumps(raw).encode()) == FORMAT_1


@pytest.mark.parametrize(
    "value",
    [True, 1.0, "1", 0, 3, -1, None, [1]],
    ids=["true", "float", "string", "zero", "three", "negative", "null", "list"],
)
def test_other_formats_are_refused_without_the_format_1_reason(
    tmp_path: Path, value: object
) -> None:
    raw = json.loads(_manifest_bytes(tmp_path))
    raw["format"] = value
    assert _refusal(kb_manifest, json.dumps(raw).encode()) == GENERIC


@pytest.mark.parametrize(
    "value",
    [False, None, 1, "true", 0],
    ids=["false", "null", "int", "string", "zero"],
)
def test_format_2_without_a_true_attestation_is_refused(tmp_path: Path, value: object) -> None:
    raw = json.loads(_manifest_bytes(tmp_path))
    raw["attests_model_host_egress"] = value
    assert _refusal(kb_manifest, json.dumps(raw).encode()) == GENERIC
    del raw["attests_model_host_egress"]
    assert _refusal(kb_manifest, json.dumps(raw).encode()) == GENERIC


def test_mutation_the_manifest_accepts_format_1_is_caught(tmp_path: Path) -> None:
    raw = json.loads(_manifest_bytes(tmp_path))
    raw["format"] = 1
    data = json.dumps(raw).encode()
    assert _refusal(kb_manifest, data) == FORMAT_1
    mutant = mutated_module(
        kb_manifest,
        (
            "    format: StrictInt = Field(ge=MANIFEST_FORMAT,",
            "    format: StrictInt = Field(ge=1,",
        ),
        (
            "            raise ManifestInvalid(FORMAT_1_REFUSED)\n",
            "            pass\n",
        ),
    )
    assert _refusal(mutant, data) is None


def test_mutation_the_attestation_not_required_is_caught(tmp_path: Path) -> None:
    raw = json.loads(_manifest_bytes(tmp_path))
    raw["attests_model_host_egress"] = False
    data = json.dumps(raw).encode()
    assert _refusal(kb_manifest, data) == GENERIC
    mutant = mutated_module(
        kb_manifest,
        (
            '        "attests_no_generated_text", "attests_no_personal_names", '
            '"attests_model_host_egress"\n',
            '        "attests_no_generated_text", "attests_no_personal_names"\n',
        ),
    )
    assert _refusal(mutant, data) is None


# --- the builder ------------------------------------------------------------------------------


def test_the_builder_refuses_a_format_1_draft_with_status_16(tmp_path: Path) -> None:
    old = draft(document())
    old["format"] = 1
    del old["attests_model_host_egress"]
    with pytest.raises(BuildRefused) as caught:
        parse_draft(json.dumps(old).encode())
    assert caught.value.outcome == DRAFT_FORMAT_1
    path = corpus(tmp_path, {"guide.txt": "## Entry\n\nEnter at the steps.\n"}, old)
    assert outcome(kb_manifest_build, path) == DRAFT_FORMAT_1


def test_mutation_the_builder_accepts_format_1_is_caught(tmp_path: Path) -> None:
    old = draft(document())
    old["format"] = 1
    path = corpus(tmp_path, {"guide.txt": "## Entry\n\nEnter at the steps.\n"}, old)
    assert outcome(kb_manifest_build, path) == DRAFT_FORMAT_1
    mutant = mutated_module(
        kb_manifest_build, ("        raise BuildRefused(FAILED_FORMAT)\n", "        pass\n")
    )
    assert outcome(mutant, path) != DRAFT_FORMAT_1


# --- the loaded version stores true -----------------------------------------------------------


def test_a_loaded_version_stores_the_attestation_as_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = Env(tmp_path / "corpus")
    env.corpus()
    fake = LoadFakeConnection()
    result = go(monkeypatch, fake, load_args(env), env.settings())
    assert result.status == 0
    inserts = [t for _, t in fake.log if "INSERT INTO public.kb_documents" in t]
    assert inserts
    for insert in inserts:
        columns, values = insert.split(" VALUES ", 1)
        assert columns.rstrip().endswith("manifest_sha256, attests_model_host_egress)")
        assert values.split(" -- ", 1)[0].rstrip().endswith(", %s, true)")


def test_mutation_the_loader_storing_false_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from divesafe.services import kb_load

    env = Env(tmp_path / "corpus")
    env.corpus()
    mutant = mutated_module(
        kb_load,
        (
            '"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s, %s, true, %s, %s, true)"',
            '"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s, %s, true, %s, %s, false)"',
        ),
    )
    fake = LoadFakeConnection()
    go(monkeypatch, fake, load_args(env), env.settings(), module=mutant)
    insert = next(t for _, t in fake.log if "INSERT INTO public.kb_documents" in t)
    assert not insert.split(" VALUES ", 1)[1].split(" -- ", 1)[0].rstrip().endswith(", true)")
