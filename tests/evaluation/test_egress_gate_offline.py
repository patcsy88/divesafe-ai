"""The live-evaluation egress gate refuses what start-up refuses (ADR 0023). Offline."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.evaluation.egress_gate import egress_gate, output_path

from divesafe.models.openai_compatible import OpenAICompatibleProvider

pytestmark = pytest.mark.safety


def _loopback() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider("m", "http://127.0.0.1:8317/v1")


def _remote() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "m", "https://llm.example.org/v1", allowed_hosts=frozenset({"llm.example.org"})
    )


FIXED_REFUSAL = "egress refused: the LLM server is not declared"


def test_an_undeclared_loopback_server_is_skipped() -> None:
    with pytest.raises(pytest.skip.Exception, match=FIXED_REFUSAL):
        egress_gate(_loopback(), allow_external=False, server_is_local=False)


def test_a_declared_or_opted_in_loopback_server_runs_and_reports_why() -> None:
    assert egress_gate(_loopback(), allow_external=False, server_is_local=True) == "declared_local"
    assert (
        egress_gate(_loopback(), allow_external=True, server_is_local=False)
        == "loopback_external_opt_in"
    )


def test_a_remote_server_needs_the_opt_in_and_refuses_the_declaration() -> None:
    with pytest.raises(pytest.skip.Exception, match=FIXED_REFUSAL):
        egress_gate(_remote(), allow_external=False, server_is_local=False)
    with pytest.raises(pytest.skip.Exception, match=FIXED_REFUSAL):
        egress_gate(_remote(), allow_external=False, server_is_local=True)
    assert (
        egress_gate(_remote(), allow_external=True, server_is_local=False)
        == "remote_external_opt_in"
    )


def test_both_flags_together_fail_instead_of_skipping() -> None:
    with pytest.raises(ValidationError, match="contradict"):
        egress_gate(_loopback(), allow_external=True, server_is_local=True)


KNOWLEDGE_URL = "https://llm.example.org/v1"
KNOWLEDGE_HOSTS = "llm.example.org,other.example.org"


def _knowledge_gate(provider: OpenAICompatibleProvider, knowledge_host: str | None) -> str:
    return egress_gate(
        provider,
        allow_external=True,
        server_is_local=False,
        base_url=KNOWLEDGE_URL,
        allowed_hosts=KNOWLEDGE_HOSTS,
        knowledge_host=knowledge_host,
    )


def test_the_knowledge_basis_only_when_the_host_is_the_urls_and_the_providers() -> None:
    """ADR 0028 decision 9: the gate builds real `Settings`, so every refusal applies."""
    assert _knowledge_gate(_remote(), "llm.example.org") == "remote_knowledge_opt_in"
    assert _knowledge_gate(_remote(), None) == "remote_external_opt_in"
    assert _knowledge_gate(_remote(), "") == "remote_external_opt_in"  # empty means absent


def test_a_provider_on_another_host_is_skipped_with_the_fixed_message() -> None:
    """Settings match; the provider calls another host: `check_egress` raises a RuntimeError,
    which skips with the fixed message (its own text, which names the setting, is not used)."""
    other = OpenAICompatibleProvider(
        "m", "https://other.example.org/v1", allowed_hosts=frozenset({"other.example.org"})
    )
    with pytest.raises(pytest.skip.Exception) as caught:
        _knowledge_gate(other, "llm.example.org")
    assert str(caught.value.msg) == (
        "egress refused: the LLM server is not declared (see ADR 0023)"
    )


@pytest.mark.parametrize(
    ("knowledge_host", "extra"),
    [
        pytest.param("other.example.org", {}, id="host-mismatch"),
        pytest.param("llm.example.org,other.example.org", {}, id="comma-list"),
        pytest.param("localhost", {}, id="loopback-name"),
        pytest.param("llm.example.org", {"server_is_local": True}, id="declared-local"),
        pytest.param("llm.example.org", {"allow_external": False}, id="no-opt-in"),
        pytest.param("llm.example.org", {"allowed_hosts": "other.example.org"}, id="not-listed"),
    ],
)
def test_a_knowledge_host_settings_refusal_fails_the_run_instead_of_skipping(
    knowledge_host: str, extra: dict[str, object]
) -> None:
    kwargs: dict[str, object] = {
        "allow_external": True,
        "server_is_local": False,
        "base_url": KNOWLEDGE_URL,
        "allowed_hosts": KNOWLEDGE_HOSTS,
        "knowledge_host": knowledge_host,
        **extra,
    }
    with pytest.raises(ValidationError):
        egress_gate(_remote(), **kwargs)  # type: ignore[arg-type]


def test_mutation_the_gate_catching_validation_error_is_caught() -> None:
    """The Settings are built before the `try`, so widening only the `except` is an equivalent
    mutant. The mutant here builds them under a handler for `ValueError` (which ValidationError
    subclasses) that skips: the mismatch must then skip instead of failing, so the test above
    fails against it."""
    from tests.evaluation import egress_gate as gate_module
    from tests.safety.kb_fakes import mutated_module

    mutant = mutated_module(
        gate_module,
        ("    settings = Settings(\n", "    try:\n      settings = Settings(\n"),
        (
            "    try:\n        return check_egress(provider, settings)\n",
            "    except ValueError:\n        pytest.skip('egress refused')\n"
            "    try:\n        return check_egress(provider, settings)\n",
        ),
    )
    with pytest.raises(pytest.skip.Exception):
        mutant.egress_gate(
            _remote(),
            allow_external=True,
            server_is_local=False,
            base_url=KNOWLEDGE_URL,
            allowed_hosts=KNOWLEDGE_HOSTS,
            knowledge_host="other.example.org",
        )


def test_the_capture_knows_the_knowledge_label() -> None:
    from tests.evaluation.capture_item6 import EGRESS_BASES

    assert {
        "none",
        "declared_local",
        "loopback_external_opt_in",
        "remote_external_opt_in",
        "remote_knowledge_opt_in",
    } == EGRESS_BASES


def test_an_output_path_never_replaces_an_existing_record(tmp_path: Path) -> None:
    first = output_path(tmp_path, "tide_check_x.json")
    assert first == tmp_path / "tide_check_x.json"
    first.write_text("earlier record")
    second = output_path(tmp_path, "tide_check_x.json")
    assert second != first and not second.exists()
    assert second.name.startswith("tide_check_x_") and second.suffix == ".json"
    assert first.read_text() == "earlier record"


def test_raw_evaluation_records_are_refused_inside_the_repository(tmp_path: Path) -> None:
    """The live evaluation writes full assessment records and raw model replies. `.gitignore` is
    only a backstop, so a directory inside the repository fails the run."""
    from tests.evaluation.egress_gate import eval_output_dir
    from tests.evaluation.output_dir import REPO, CaptureRefused

    with pytest.raises(CaptureRefused, match="inside the repository"):
        eval_output_dir(str(REPO), tmp_path)
    with pytest.raises(CaptureRefused, match="inside the repository"):
        eval_output_dir(str(REPO / "tests" / "evaluation"), tmp_path)
    with pytest.raises(CaptureRefused, match="inside the repository"):
        eval_output_dir(None, REPO / "tests")
    assert eval_output_dir(None, tmp_path) == tmp_path.resolve()
    assert eval_output_dir(str(tmp_path), REPO) == tmp_path.resolve()


def test_a_raw_record_is_written_for_the_owner_only_and_never_replaced(tmp_path: Path) -> None:
    import stat

    from tests.evaluation.egress_gate import write_private

    path = tmp_path / "record.json"
    write_private(path, "raw")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_private(path, "second")
    assert path.read_text() == "raw"


def test_a_missing_output_directory_is_created_privately_before_any_prompt(tmp_path: Path) -> None:
    import stat

    from tests.evaluation.egress_gate import eval_output_dir

    target = tmp_path / "a" / "b"
    assert eval_output_dir(str(target), tmp_path) == target.resolve()
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


_NEUTRAL = ("egress_gate", "output_dir")
_FORBIDDEN = ("tests.evaluation.capture_item6", "divesafe.api")


def _forbidden_imports(source: str) -> list[str]:
    import ast

    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        else:
            continue
        found += [n for n in names if n.startswith(_FORBIDDEN) or n.endswith("capture_item6")]
    return found


@pytest.mark.parametrize("name", _NEUTRAL)
def test_the_output_dir_path_never_imports_the_capture_or_the_api(name: str) -> None:
    """ADR 0030 "AST tests": `eval_output_dir` loads neither `capture_item6` nor `api.state`."""
    source = (Path(__file__).parent / f"{name}.py").read_text(encoding="utf-8")
    assert _forbidden_imports(source) == []


@pytest.mark.parametrize(
    "line",
    [
        "from tests.evaluation.capture_item6 import check_output_dir",
        "from tests.evaluation import capture_item6",
        "import tests.evaluation.capture_item6",
        "from divesafe.api.state import build_default_state",
        "from divesafe.api import state",
    ],
)
def test_mutation_a_capture_or_api_import_is_caught(line: str) -> None:
    source = (Path(__file__).parent / "egress_gate.py").read_text(encoding="utf-8")
    assert _forbidden_imports(f"{source}\n{line}\n") != []


def _modules_loaded_by_eval_output_dir(tmp_path: Path, prefix: str = "") -> str:
    """Run `eval_output_dir` in a fresh interpreter; print the capture or API modules it loaded."""
    import subprocess
    import sys

    probe = (
        "import sys, pathlib, tests.evaluation.egress_gate as g\n"
        f"{prefix}"
        "g.eval_output_dir(None, pathlib.Path(sys.argv[1]) / 'out')\n"
        "bad = [m for m in sys.modules\n"
        "       if m.endswith('capture_item6') or m.startswith('divesafe.api')]\n"
        "print(bad)\n"
    )
    root = Path(__file__).resolve().parents[2]
    done = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path)],
        cwd=root,
        env={"PYTHONPATH": f"{root / 'src'}:{root}", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return done.stdout.strip()


def test_using_eval_output_dir_loads_neither_the_capture_nor_the_api(tmp_path: Path) -> None:
    assert _modules_loaded_by_eval_output_dir(tmp_path) == "[]"


@pytest.mark.parametrize(
    "prefix", ["import tests.evaluation.capture_item6\n", "import divesafe.api.state\n"]
)
def test_mutation_the_probe_sees_a_loaded_capture_or_api(tmp_path: Path, prefix: str) -> None:
    assert _modules_loaded_by_eval_output_dir(tmp_path, prefix) != "[]"
