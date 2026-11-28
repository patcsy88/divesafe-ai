"""CI pulls the two images the Caddy forwarding test needs by extracting them with sed (owner
#149b, .github/workflows/ci.yml). These are the same two expressions in Python, so a reformat that
breaks the extraction (a wrapped line, a moved quote, a dropped digest) fails here, offline."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker" / "docker-compose.production.yml"
LIVE = ROOT / "tests" / "integration" / "test_caddy_forwarding_live.py"
CI = ROOT / ".github" / "workflows" / "ci.yml"

# sed -n 's/^    image: \(caddy:[^ ]*@sha256:[0-9a-f]\{64\}\)$/\1/p'
CADDY = re.compile(r"^    image: (caddy:[^ ]*@sha256:[0-9a-f]{64})$")
# sed -n 's/^NODE_IMAGE = "\(node:[^"]*@sha256:[0-9a-f]\{64\}\)"$/\1/p'
NODE = re.compile(r'^NODE_IMAGE = "(node:[^"]*@sha256:[0-9a-f]{64})"$')


def _extract(pattern: re.Pattern[str], path: Path) -> list[str]:
    # sed works line by line on the raw file, comments included.
    return [m.group(1) for line in path.read_text().split("\n") if (m := pattern.match(line))]


def test_ci_extracts_exactly_one_caddy_image_from_compose() -> None:
    assert _extract(CADDY, COMPOSE) == [
        "caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0"
    ]


def test_ci_extracts_exactly_one_node_image_from_the_live_test() -> None:
    assert _extract(NODE, LIVE) == [
        "node:26-alpine@sha256:8996bb5821d62a8fd9bb27c1cb07462f3fdce6e47d640c6a881ead0b064ce216"
    ]


def test_the_extracted_images_are_the_ones_the_live_test_runs() -> None:
    from tests.integration.test_caddy_forwarding_live import NODE_IMAGE, _caddy_image

    assert _extract(NODE, LIVE) == [NODE_IMAGE]
    assert _extract(CADDY, COMPOSE) == [_caddy_image()]


def test_ci_uses_these_same_two_expressions() -> None:
    """If the workflow's sed changes, this file must change with it."""
    ci = CI.read_text()
    assert (
        r"sed -n 's/^    image: \(caddy:[^ ]*@sha256:[0-9a-f]\{64\}\)$/\1/p' "
        "docker/docker-compose.production.yml" in ci
    )
    assert (
        r"""sed -n 's/^NODE_IMAGE = "\(node:[^"]*@sha256:[0-9a-f]\{64\}\)"$/\1/p' """
        "tests/integration/test_caddy_forwarding_live.py" in ci
    )
    assert "! grep -q 'caddy forwarding cannot run:' pytest.log" in ci


def test_the_extraction_rejects_a_wrapped_or_unpinned_line() -> None:
    good = 'NODE_IMAGE = "node:26-alpine@sha256:' + "a" * 64 + '"'
    assert NODE.match(good)
    for bad in (
        'NODE_IMAGE = "node:26-alpine"',
        good + "  # comment",
        good.replace('"', "'"),
        "NODE_IMAGE = (",
        good[:-2] + '"',
    ):
        assert not NODE.match(bad), bad
