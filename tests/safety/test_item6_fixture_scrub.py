"""ADR 0025 item 6: recorded-trace fixtures must carry no raw-run material. Offline.

The rules live in `tests/evaluation/item6_scrub.py`, shared with the capture's write-time check, so
the fixture scan and the capture cannot drift apart. The scan covers `tests/fixtures` and any item 6
fixture directory. It is a screen, not a proof: a paraphrase is not detected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.evaluation.item6_scrub import REPO, RULES, scan_dirs, scan_text

pytestmark = pytest.mark.safety

SCAN_DIRS = (REPO / "tests" / "fixtures", REPO / "tests" / "fixtures" / "item6")


def test_the_scan_has_every_rule_the_adr_lists() -> None:
    assert {name for name, _ in RULES} == {
        "ip_address",
        "loopback_literal",
        "proxy_software",
        "bearer_or_key",
        "token_or_key_field",
        "http_url",
        "provider_metadata_key",
        "users_path",
        "met_token_label",
        "host_port",
    }


def test_the_current_fixtures_are_clean() -> None:
    assert scan_dirs(SCAN_DIRS) == []


def test_a_planted_bad_fixture_in_a_directory_is_flagged(tmp_path: Path) -> None:
    planted = tmp_path / "item6"
    planted.mkdir()
    (planted / "reply.json").write_text(
        '{"id": "chatcmpl-abc", "system_fingerprint": "fp_1", "created": 1760000000,'
        ' "url": "https://wall-e.example.org/v1", "ip": "203.0.113.7",'
        ' "proxy": "localhost:8317", "header": "Bearer sk-ABCDEFGH12345678",'
        ' "path": "/Users/someone/project"}',
        encoding="utf-8",
    )
    hits = scan_dirs((planted,))
    assert hits, "a planted fixture with raw-run material must be flagged"
    flagged = {line.rsplit(": ", 1)[1] for line in hits}
    assert {
        "ip_address",
        "loopback_literal",
        "bearer_or_key",
        "http_url",
        "provider_metadata_key",
        "users_path",
    } <= flagged


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("forwarded through CLIProxyAPI", "proxy_software"),
        ("cliproxy", "proxy_software"),
        ('{"api_key": "abc"}', "token_or_key_field"),
        ('"token": "x"', "token_or_key_field"),
        ("Authorization: Basic x", "token_or_key_field"),
        ('"system_fingerprint": "fp_9"', "provider_metadata_key"),
        ('"created": 1760000000', "provider_metadata_key"),
        ("id chatcmpl-123", "provider_metadata_key"),
        ("connect to 10.0.0.5 next", "ip_address"),
        ("see http://example.org", "http_url"),
        ("port localhost:8317", "loopback_literal"),
        ("see llm.example.org:8317 for the model", "host_port"),
        ("open 203.0.113.7:8080", "host_port"),
        ("stored under /home/someone/capture", "users_path"),
        ("stored under /private/var/folders/x", "users_path"),
        ("METToken:abc123", "met_token_label"),
    ],
)
def test_each_scrub_rule_catches_its_own_pattern(text: str, rule: str) -> None:
    assert rule in scan_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "Pulau Tioman, 2026-10-17, heights in metres",
        "LOCATION:499",
        "version 1.2",
    ],
)
def test_ordinary_recorded_text_is_not_flagged(text: str) -> None:
    assert scan_text(text) == []


@pytest.mark.parametrize("suffix", [".bin", ".log", ".dat", ".json"])
def test_a_planted_secret_in_any_file_suffix_is_flagged(tmp_path: Path, suffix: str) -> None:
    # F10: the scan covers every file in the directory, not only the four text suffixes.
    planted = tmp_path / "item6"
    planted.mkdir()
    (planted / f"stray{suffix}").write_text(
        "Authorization: Bearer sk-ABCDEFGH12345678 via llm-gw.example.org:8317",
        encoding="utf-8",
    )
    hits = scan_dirs((planted,))
    assert hits, f"a planted {suffix} file with a secret must be flagged"
    assert any("bearer_or_key" in line for line in hits)
