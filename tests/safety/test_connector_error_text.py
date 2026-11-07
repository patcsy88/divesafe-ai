"""Connector error text reaches the issue list, the stage trace and the human. It must hold only
fixed wording, labels chosen by the code, or provider text passed through `clip` (bounded and
printable). A raw provider value in an f-string would put unbounded text into a record."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.safety

DATA = Path(__file__).resolve().parents[2] / "src" / "divesafe" / "data"
# Names whose values the code chooses (field labels, constants, computed dates and counts).
CODE_CHOSEN = frozenset(
    {
        "label", "name", "host", "datatype", "unit", "day", "MAX_TEXT", "SLOTS", "max_bytes",
        "missing[0]", "absent[0]", "group.name", "exc.code", "_label(url)", "parsed.hostname",
    }
)  # fmt: skip


def _interpolations(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
            continue
        func = node.exc.func
        name = getattr(func, "id", getattr(func, "attr", ""))
        if not (name.startswith("Connector") and name.endswith("Error")):
            continue
        for arg in node.exc.args:
            if isinstance(arg, ast.JoinedStr):
                for part in arg.values:
                    if isinstance(part, ast.FormattedValue):
                        found.append((node.lineno, ast.unparse(part.value)))
    return found


def _unsafe(source: str) -> list[str]:
    return [
        f"line {line}: {expr}"
        for line, expr in _interpolations(ast.parse(source))
        if not expr.startswith("clip(") and expr not in CODE_CHOSEN
    ]


def test_connector_errors_interpolate_only_clipped_or_code_chosen_values() -> None:
    problems: list[str] = []
    scanned = 0
    for path in sorted(DATA.glob("*.py")):
        scanned += len(_interpolations(ast.parse(path.read_text())))
        problems += [f"{path.name} {p}" for p in _unsafe(path.read_text())]
    assert scanned > 20  # the scan still finds the f-strings it is meant to check
    assert problems == []


def test_the_scan_flags_a_raw_provider_value() -> None:
    bad = 'raise ConnectorResponseError(f"provider said {body}")'
    assert _unsafe(bad) == ["line 1: body"]
    assert _unsafe('raise ConnectorResponseError(f"provider said {clip(body)}")') == []
