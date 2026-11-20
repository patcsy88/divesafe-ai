"""Offline tests of ADR 0027 §2's live-check record (risk S5). The sender is not ready until the
record is in ADR 0027; these tests are in the commit the live check runs from and FAIL (never skip,
never xfail) until a docs-only change adds the record. They then pin the sender's constants to
what the live check saw against the real server, and freeze it by file hashes (owner decision,
2026-11-20): every file the record lists in `sender_sha256` still has the recorded SHA-256
(otherwise the live check is run again and the record updated). No git history is read, so the
freeze holds in a shallow checkout and after a squash or rebase merge.

The record format (fixed; the owner reviews it): in ADR 0027, a line that is exactly
`#### Live check record`, inside §2, followed by one fenced block opened by exactly
```` ```json ```` and holding one JSON object with exactly these keys:

    {
      "commit": "<40 lowercase hex: `git -C <root> rev-parse HEAD` the live check ran from>",
      "langfuse_version": "5.2.0",
      "base_url": "http://127.0.0.1:3000",
      "projects_read": ["divesafe-eval"],
      "otlp_path": "/api/public/otel/v1/traces",
      "otlp_status": 200,
      "dataset_run_link_as_span_attributes": true,
      "calls": {
        "projects": ["GET", "/api/public/projects", 200, "Projects"],
        "dataset": ["POST", "/api/public/v2/datasets", 200, "Dataset"],
        "dataset_item": ["POST", "/api/public/dataset-items", 200, "DatasetItem"],
        "scores": ["POST", "/api/public/scores", 202, "CreateScoreBatchResponse"]
      },
      "freeze_installed": true,
      "extra_packages": {"pytest": "<version>"},
      "dataset_create_existing_status": <int HTTP status, 100-599>,
      "experiment_id_equals_server_run_id": <true or false>,
      "langfuse_server_version": "<the health reply's version, as printed by the live check>",
      "sender_sha256": {
        "tests/evaluation/langfuse_sender.py": "<64 lowercase hex>",
        "tests/evaluation/langfuse_checks.py": "<64 lowercase hex>",
        "tests/evaluation/code_scores.py": "<64 lowercase hex>",
        "tests/evaluation/harness.py": "<64 lowercase hex>",
        "tests/evaluation/output_dir.py": "<64 lowercase hex>",
        "tests/evaluation/egress_gate.py": "<64 lowercase hex>",
        "tests/evaluation/ablation.py": "<64 lowercase hex>",
        "pyproject.toml#eval:langfuse": "<64 lowercase hex>",
        "tests/fixtures/open_meteo_marine_tioman_recorded_2026-10-08.json": "<64 lowercase hex>",
        "tests/fixtures/data_gov_my_warning_recorded_2026-10-09.json": "<64 lowercase hex>"
      }
    }

`calls` holds, for each `client.api` call a full run makes (exactly these four), the method, path,
status and return type the live check saw. `freeze_installed` is true only if every package of the
build-step-1 freeze was installed at its version; `extra_packages` lists every other package in
that environment with its version (each required only by `[dev]`, checked at the live check). Two
keys are recorded only, never compared with a sender constant (owner, 2026-11-20, risk-reviewer
N5): `dataset_create_existing_status`, the HTTP status the server gave a dataset create for a name
that already exists (an int, 100-599), and `experiment_id_equals_server_run_id`, whether the
sender's derived experiment id equalled the server's dataset run id (a JSON bool). A third is
record-only too: `langfuse_server_version`, the Langfuse server version from the live check's
health call (`HEALTH_CALL`, made on the live check only, so it is not in `calls`), a string that
fully matches the version shape of `_SERVER_VERSION` below (an optional `v`, two to four dot-
separated groups of 1-4 digits, then optionally `-` or `+` and 1-20 of `[0-9A-Za-z.]`; a literal
copy of `langfuse_checks._SERVER_VERSION`, security round 4 S1: no Langfuse key fits it) and
holds no `pk-lf` / `sk-lf`, ignoring case.

`commit` is information only (the commit the live check ran from); it is never used for ancestry.
`sender_sha256` maps a repository-relative POSIX path to the lowercase 64-hex SHA-256 of that
file's bytes. It must hold at least the paths in `REQUIRED_HASHED`; more paths may be listed and
every listed one is checked. The `langfuse` pin is not a file: its entry is keyed exactly
`pyproject.toml#eval:langfuse`, and its value is the SHA-256 of the UTF-8 bytes of the one
requirement string in `[project.optional-dependencies] eval` whose name is `langfuse`, exactly as
written there (read with `tomllib`, no whitespace or case change), today `langfuse==5.2.0`. So
only a change to that pin needs a new record, not an unrelated edit elsewhere in `pyproject.toml`.
Compute a value with `printf '%s' 'langfuse==5.2.0' | shasum -a 256` (no trailing newline) and the
file values with `shasum -a 256 <path>`.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from tests.evaluation import langfuse_checks, langfuse_sender

pytestmark = pytest.mark.safety

_ROOT = Path(__file__).resolve().parents[2]
_ADR = _ROOT / "docs" / "adr" / "0027-observability.md"
HEADING = "#### Live check record"
RECORD_KEYS = frozenset(
    {
        "commit",
        "langfuse_version",
        "base_url",
        "projects_read",
        "otlp_path",
        "otlp_status",
        "dataset_run_link_as_span_attributes",
        "calls",
        "freeze_installed",
        "extra_packages",
        "dataset_create_existing_status",
        "experiment_id_equals_server_run_id",
        "langfuse_server_version",
        "sender_sha256",
    }
)
PIN_KEY = "pyproject.toml#eval:langfuse"
REQUIRED_HASHED = frozenset(
    {
        "tests/evaluation/langfuse_sender.py",
        "tests/evaluation/langfuse_checks.py",
        "tests/evaluation/code_scores.py",
        "tests/evaluation/harness.py",
        "tests/evaluation/output_dir.py",
        "tests/evaluation/egress_gate.py",
        "tests/evaluation/ablation.py",
        PIN_KEY,
        *(f"tests/fixtures/{name}" for name in langfuse_checks.FIXTURE_SHA256),
    }
)
CALL_CONSTANTS = {
    "projects": langfuse_sender.PROJECTS_CALL,
    "dataset": langfuse_sender.DATASET_CALL,
    "dataset_item": langfuse_sender.DATASET_ITEM_CALL,
    "scores": langfuse_sender.SCORES_CALL,
}
_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# A literal copy, so a loosened sender pattern does not loosen this test.
_SERVER_VERSION = re.compile(r"v?[0-9]{1,4}(\.[0-9]{1,4}){1,3}([-+][0-9A-Za-z.]{1,20})?")
_PIN_NAME = re.compile(r"langfuse(?![A-Za-z0-9._-])")
_SECTION_2 = "### 2. Evaluation"
_SECTION_3 = "### 3. Prompt mirror"


def _adr() -> str:
    return _ADR.read_text(encoding="utf-8")


def _section_2(text: str) -> str:
    start, end = text.find(_SECTION_2), text.find(_SECTION_3)
    assert 0 <= start < end, "ADR 0027 §2 not found"
    return text[start:end]


def _record() -> dict[str, Any]:
    """The record, or a test failure (never a skip) saying what is missing."""
    section = _section_2(_adr())
    lines = section.splitlines()
    at = [i for i, line in enumerate(lines) if line.strip() == HEADING]
    if not at:
        pytest.fail(f"no live-check record: ADR 0027 §2 has no {HEADING!r} heading")
    if len(at) > 1:
        pytest.fail(f"ADR 0027 §2 has {len(at)} {HEADING!r} headings; exactly one is allowed")
    rest = lines[at[0] + 1 :]
    opened = next((i for i, line in enumerate(rest) if line.strip()), None)
    if opened is None or rest[opened].strip() != "```json":
        pytest.fail(f"the first line after {HEADING!r} is not a ```json fence")
    closed = next((i for i in range(opened + 1, len(rest)) if rest[i].strip() == "```"), None)
    if closed is None:
        pytest.fail("the live-check record's ```json fence is never closed")
    try:
        record = json.loads("\n".join(rest[opened + 1 : closed]))
    except json.JSONDecodeError:
        pytest.fail("the live-check record is not valid JSON")
    if not isinstance(record, dict):
        pytest.fail("the live-check record is not a JSON object")
    return record


def _build_step_bullet() -> str:
    section = _section_2(_adr())
    start = section.find("- **Build step 1")
    end = section.find("- **Testing.**", start)
    assert 0 <= start < end, "ADR 0027 §2's build-step-1 bullet not found"
    return section[start:end]


def test_adr_0027_live_check_is_no_longer_pending() -> None:
    """ADR 0027 §2: the bullet names the verified version and no longer says PENDING."""
    bullet = _build_step_bullet()
    assert f"`langfuse=={langfuse_checks.LANGFUSE_VERIFIED_VERSION}`" in bullet
    # The status sentence ("... is PENDING"); the bullet's own description of this test ("no
    # longer says PENDING", "PENDING put back") stays.
    pending = re.findall(r"\bis\s+PENDING\b", bullet)
    assert pending == [], "ADR 0027 §2 still says the live check against divesafe-eval is PENDING"


def test_the_live_check_record_has_exactly_the_fixed_keys() -> None:
    record = _record()
    assert set(record) == RECORD_KEYS
    assert isinstance(record["commit"], str) and _COMMIT.fullmatch(record["commit"]), (
        "the record's commit is not 40 lowercase hex characters"
    )
    assert record["freeze_installed"] is True
    extras = record["extra_packages"]
    assert isinstance(extras, dict)
    assert all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in extras.items())
    assert "langfuse" not in {k.lower() for k in extras}
    # Record-only keys (risk-reviewer N5): typed, never compared with a constant. `bool` is a
    # subclass of `int`, so the status is checked by exact type.
    status = record["dataset_create_existing_status"]
    assert type(status) is int and 100 <= status <= 599, (
        "dataset_create_existing_status is not an int HTTP status (100-599)"
    )
    assert type(record["experiment_id_equals_server_run_id"]) is bool, (
        "experiment_id_equals_server_run_id is not a JSON bool"
    )
    version = record["langfuse_server_version"]
    assert type(version) is str and _SERVER_VERSION.fullmatch(version), (
        "langfuse_server_version is not a version string of the fixed shape"
    )
    assert "pk-lf" not in version.lower() and "sk-lf" not in version.lower(), (
        "langfuse_server_version holds a key prefix"
    )
    _assert_hash_map_shape(record["sender_sha256"])


def test_the_sender_constants_equal_the_live_check_record() -> None:
    """What the live check saw against the real server equals what the sender expects."""
    record = _record()
    assert record["langfuse_version"] == langfuse_checks.LANGFUSE_VERIFIED_VERSION
    assert record["base_url"] == langfuse_checks.LANGFUSE_BASE_URL
    assert record["projects_read"] == [langfuse_checks.LANGFUSE_PROJECT]
    assert record["otlp_path"] == langfuse_checks.OTLP_TRACES_PATH
    assert record["otlp_status"] == langfuse_sender.SPAN_STATUS
    assert record["dataset_run_link_as_span_attributes"] is True
    calls = record["calls"]
    assert isinstance(calls, dict) and set(calls) == set(CALL_CONSTANTS)
    for name, constant in CALL_CONSTANTS.items():
        assert tuple(calls[name]) == constant, name


def _pin_requirement() -> str:
    """The one `langfuse` requirement string of the `eval` extra, exactly as written."""
    with (_ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    extra = project["optional-dependencies"]["eval"]
    pins = [r for r in extra if isinstance(r, str) and _PIN_NAME.match(r.strip())]
    assert len(pins) == 1, f"the eval extra has {len(pins)} langfuse requirements, not one"
    return str(pins[0])


def _current_sha256(key: str) -> str | None:
    """The hash of what `key` names now, or None when the file is missing."""
    if key == PIN_KEY:
        return hashlib.sha256(_pin_requirement().encode("utf-8")).hexdigest()
    path = _ROOT / key
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_plain_path(key: str, root: Path) -> None:
    """A repository-relative POSIX path written one way only, that stays in the repository after
    every symlink is resolved."""
    posix = PurePosixPath(key)
    assert key, "sender_sha256 has an empty path"
    assert "\\" not in key, f"sender_sha256 key {key!r} holds a backslash"
    assert not posix.is_absolute(), f"sender_sha256 key {key!r} is absolute"
    assert ".." not in posix.parts, f"sender_sha256 key {key!r} holds '..'"
    # `PurePosixPath` drops `./`, `//` and a trailing `/`; any of them makes the text differ.
    assert posix.as_posix() == key, f"sender_sha256 key {key!r} is not in plain form"
    assert (root / key).resolve().is_relative_to(root.resolve()), f"{key!r} leaves the repository"


def _assert_hash_map_shape(hashes: object, root: Path = _ROOT) -> dict[str, str]:
    assert isinstance(hashes, dict) and hashes, "sender_sha256 is not a non-empty JSON object"
    for key, value in hashes.items():
        assert isinstance(value, str) and _SHA256.fullmatch(value), (
            f"sender_sha256[{key!r}] is not 64 lowercase hex characters"
        )
        if key != PIN_KEY:
            _assert_plain_path(key, root)
    missing = sorted(REQUIRED_HASHED - set(hashes))
    assert missing == [], f"sender_sha256 lacks the required paths {missing}"
    return hashes


def test_the_pin_hash_input_is_the_exact_eval_requirement() -> None:
    """The documented input of the pin's hash, and the required paths, exist today (this test
    needs no record)."""
    assert _pin_requirement() == f"langfuse=={langfuse_checks.LANGFUSE_VERIFIED_VERSION}"
    for key in REQUIRED_HASHED - {PIN_KEY}:
        assert (_ROOT / key).is_file(), key
    assert len(REQUIRED_HASHED) == 10


def test_every_file_the_record_hashes_is_unchanged() -> None:
    """ADR 0027 §2 (owner decision, 2026-11-20): the record is frozen by file hashes, not git
    history. Every listed path is recomputed; a changed or missing file, or a required path
    absent from the record, means the live check is run again and the record updated."""
    record = _record()
    if "sender_sha256" not in record:
        pytest.fail("the live-check record has no sender_sha256")
    hashes = _assert_hash_map_shape(record["sender_sha256"])
    changed = []
    for key, recorded in sorted(hashes.items()):
        now = _current_sha256(key)
        if now is None:
            changed.append(f"{key}: missing")
        elif now != recorded:
            changed.append(f"{key}: changed since the live check")
    assert changed == [], f"files changed after the live check: {changed}"


def _good_map() -> dict[str, str]:
    return {key: "0" * 64 for key in REQUIRED_HASHED}


# Each bad form, and the check that must refuse it (so dropping any one check is seen even when a
# later check would also refuse the form).
_BAD_PATHS = {
    "absolute": ("/etc/passwd", "is absolute"),
    "absolute_inside": ("<root>/inside/a.py", "is absolute"),
    "dotdot": ("../x", "holds '..'"),
    "dotdot_inside": ("inside/../inside/a.py", "holds '..'"),
    "backslash": ("inside\\a.py", "holds a backslash"),
    "dot_slash": ("./inside/a.py", "is not in plain form"),
    "double_slash": ("inside//a.py", "is not in plain form"),
    "trailing_slash": ("inside/", "is not in plain form"),
    "symlink_outside": ("outside_link/x", "leaves the repository"),
    "empty": ("", "has an empty path"),
}


@pytest.mark.parametrize("form", sorted(_BAD_PATHS))
def test_a_hash_map_with_a_bad_path_is_refused(tmp_path: Path, form: str) -> None:
    """Security nit 3: every listed path is a plain repository-relative path that stays inside the
    repository (needs no record). The root is a temporary repository whose `outside_link` points
    outside it; `<root>` stands for that root's absolute path."""
    root = tmp_path / "repo"
    (root / "inside").mkdir(parents=True)
    (root / "inside" / "a.py").write_text("a")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "x").write_text("x")
    (root / "outside_link").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    key, reason = _BAD_PATHS[form]
    hashes = {**_good_map(), key.replace("<root>", str(root)): "0" * 64}

    with pytest.raises(AssertionError) as refused:
        _assert_hash_map_shape(hashes, root)

    assert reason in str(refused.value), str(refused.value)


def test_a_good_hash_map_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "inside").mkdir(parents=True)
    (root / "link").symlink_to(root / "inside", target_is_directory=True)
    hashes = {**_good_map(), "inside/a.py": "f" * 64, "link/a.py": "a" * 64}

    assert _assert_hash_map_shape(hashes, root) == hashes
    assert _assert_hash_map_shape(_good_map()) == _good_map()
