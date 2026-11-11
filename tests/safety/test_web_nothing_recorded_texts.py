"""The web app says "Nothing was recorded" for a 503 only when the API's detail is one of its own
texts that certainly recorded nothing (review of a34f8c9). Drift between the two lists would
make the web app over-warn silently; this test makes it fail instead."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.safety

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "src" / "divesafe" / "api" / "app.py"
WEB = ROOT / "web" / "lib" / "api.ts"


def _constants(tree: ast.Module) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found[node.targets[0].id] = node.value.value
    return found


def _text(node: ast.expr, constants: dict[str, str]) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    raise AssertionError(f"a 503 detail that is not a fixed text: {ast.dump(node)}")


def api_503_details(source: str) -> set[str]:
    """Every detail the API sends with a 503, from `HTTPException(status_code=503, detail=...)`
    and `JSONResponse({"detail": ...}, status_code=503)`."""
    tree = ast.parse(source)
    constants = _constants(tree)
    details: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        keywords = {k.arg: k.value for k in node.keywords}
        status = keywords.get("status_code")
        if not (isinstance(status, ast.Constant) and status.value == 503):
            continue
        if "detail" in keywords:
            details.add(_text(keywords["detail"], constants))
        elif node.args and isinstance(node.args[0], ast.Dict):
            body = node.args[0]
            for key, value in zip(body.keys, body.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "detail":
                    details.add(_text(value, constants))
        else:
            raise AssertionError(f"a 503 whose detail this test cannot read: {ast.dump(node)}")
    return details


def web_nothing_recorded(source: str) -> set[str]:
    match = re.search(r"const NOTHING_RECORDED_503 = new Set\(\[(.*?)\]\);", source, re.S)
    assert match is not None, "web/lib/api.ts no longer defines NOTHING_RECORDED_503"
    return set(re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1)))


def test_the_web_app_names_exactly_the_apis_503_texts_that_recorded_nothing() -> None:
    details = api_503_details(APP.read_text(encoding="utf-8"))
    certain = {d for d in details if "nothing was recorded" in d}
    assert len(certain) == 4
    assert all("outcome is unknown" not in d for d in certain)
    assert web_nothing_recorded(WEB.read_text(encoding="utf-8")) == certain


def test_the_scan_sees_every_503_the_api_sends() -> None:
    """Positive control: the unknown-outcome and read texts are found too, and not listed."""
    details = api_503_details(APP.read_text(encoding="utf-8"))
    assert (
        "storage is unavailable; the outcome is unknown: reload to check before trying again"
        in (details)
    )
    assert "storage is unavailable; try again later" in details
    assert "the outcome is unknown; reload to see whether it was recorded" in details
    assert len(details) == 7


@pytest.mark.parametrize(
    ("target", "edit"),
    [
        ("web", ('  "server configuration is unsafe; nothing was recorded",\n', "")),
        ("web", ('  "storage is unavailable; nothing was recorded",\n', '  "storage is down",\n')),
        (
            "web",
            (
                '  "server configuration is unsafe; nothing was recorded",\n',
                '  "server configuration is unsafe; nothing was recorded",\n'
                '  "storage is unavailable; try again later",\n',
            ),
        ),
        (
            "app",
            (
                'detail="server configuration is unsafe; nothing was recorded"',
                'detail="server configuration is not safe; nothing was recorded"',
            ),
        ),
    ],
    ids=["web-drops-one", "web-renames-one", "web-adds-the-read-text", "api-renames-one"],
)
def test_mutation_drift_between_the_lists_is_caught(target: str, edit: tuple[str, str]) -> None:
    app = APP.read_text(encoding="utf-8")
    web = WEB.read_text(encoding="utf-8")
    old, new = edit
    if target == "web":
        assert web.count(old) == 1
        web = web.replace(old, new)
    else:
        assert app.count(old) == 1
        app = app.replace(old, new)
    certain = {d for d in api_503_details(app) if "nothing was recorded" in d}
    assert web_nothing_recorded(web) != certain
