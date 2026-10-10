"""Loading a reviewed ruleset file, and the production wiring. All values are SYNTHETIC."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import NOW
from tests.safety.test_rule_definitions import EXPIRES, raw

from divesafe.api.state import build_engine
from divesafe.config import Settings
from divesafe.data import TIOMAN_ISLAND
from divesafe.domain import RiskFactorKind
from divesafe.orchestration import (
    RulesetError,
    load_ruleset,
    load_ruleset_bytes,
    read_ruleset_file,
)

NOW_AFTER_SIGNOFF = NOW
SITE = TIOMAN_ISLAND.id


def tioman(**changes: Any) -> dict[str, Any]:
    """A synthetic definition scoped to the one registered site."""
    changes.setdefault("scope", {"site_ids": [SITE]})
    return raw(**changes)


def _file(*definitions: dict[str, Any], version: str = "synthetic-ruleset-1") -> str:
    return json.dumps({"ruleset_version": version, "definitions": list(definitions)})


def test_a_clean_file_loads_its_rules_and_version() -> None:
    loaded = load_ruleset(_file(raw()), NOW_AFTER_SIGNOFF)
    assert loaded.is_clean and loaded.version == "synthetic-ruleset-1"
    assert [r.rule_id for r in loaded.rules] == ["definition.synthetic.wave_height"]


def test_an_empty_ruleset_is_valid_and_defines_nothing() -> None:
    loaded = load_ruleset(_file(), NOW_AFTER_SIGNOFF)
    assert loaded.is_clean and loaded.rules == ()


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        "{}",
        '{"ruleset_version": "v"}',
        '{"ruleset_version": " ", "definitions": []}',
        '{"ruleset_version": "v", "definitions": {}}',
        "{" * 5000,
    ],
)
def test_a_file_that_is_unusable_as_a_whole_is_an_error(text: str) -> None:
    with pytest.raises(RulesetError):
        load_ruleset(text, NOW_AFTER_SIGNOFF)


def test_oversized_files_and_too_many_definitions_are_refused() -> None:
    with pytest.raises(RulesetError):
        load_ruleset(_file(raw(), version="x" * 2_000_000), NOW_AFTER_SIGNOFF)
    many = [raw(id=f"synthetic.rule-{i}") for i in range(501)]
    with pytest.raises(RulesetError, match="too many"):
        load_ruleset(_file(*many), NOW_AFTER_SIGNOFF)


def test_an_unreviewed_draft_is_refused_not_silently_skipped() -> None:
    draft = raw(status="REQUIRES DOMAIN VALIDATION")
    loaded = load_ruleset(_file(draft), NOW_AFTER_SIGNOFF)
    assert not loaded.is_clean and loaded.rules == ()
    assert "not VALIDATED" in loaded.refused[0].reason


def test_an_expired_definition_is_refused_at_load() -> None:
    loaded = load_ruleset(_file(raw()), EXPIRES + timedelta(seconds=1))
    assert not loaded.is_clean and "expired" in loaded.refused[0].reason


def test_a_duplicate_id_is_refused() -> None:
    loaded = load_ruleset(_file(raw(), raw()), NOW_AFTER_SIGNOFF)
    assert len(loaded.rules) == 1 and loaded.refused[0].reason == "duplicate definition id"


def test_a_bad_definition_is_refused_individually_and_the_good_ones_still_load() -> None:
    good = raw(id="synthetic.good")
    bad = raw(id="synthetic.bad", unit="ft")
    loaded = load_ruleset(_file(good, bad), NOW_AFTER_SIGNOFF)
    assert [r.rule_id for r in loaded.rules] == ["definition.synthetic.good"]
    assert loaded.refused[0].definition_id == "synthetic.bad" and not loaded.is_clean


def test_refusal_reasons_never_echo_the_submitted_values() -> None:
    secret = "SUBMITTED-SECRET-VALUE-123"
    bad = raw(id="synthetic.leak", unit=secret, factor="wind")
    bad["source"] = {"document": secret}  # also malformed
    loaded = load_ruleset(_file(bad), NOW_AFTER_SIGNOFF)
    assert loaded.refused and secret not in " ".join(r.reason for r in loaded.refused)
    assert all(len(r.reason) < 1200 for r in loaded.refused)


def test_a_definition_for_a_factor_with_no_data_source_is_refused_with_the_reason() -> None:
    loaded = load_ruleset(_file(raw(factor="wind")), NOW_AFTER_SIGNOFF)
    assert "no data source" in loaded.refused[0].reason


def test_a_definition_listing_unrepresentable_conditions_is_refused() -> None:
    loaded = load_ruleset(
        _file(raw(unrepresented_conditions=["diver qualification"])), NOW_AFTER_SIGNOFF
    )
    assert not loaded.is_clean and "cannot be VALIDATED" in loaded.refused[0].reason


# --- production wiring -----------------------------------------------------------------------


def _settings(path: Path | None = None) -> Settings:
    return Settings(evidence_max_age_minutes=60, ruleset_path=path)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "ruleset.json"
    path.write_text(text)
    return path


def test_without_a_ruleset_the_engine_is_interim_with_placeholders_for_every_factor() -> None:
    engine = build_engine(_settings(), now=NOW)
    assert engine is not None
    ids = {getattr(r, "rule_id", "") for r in engine._rules}
    assert all(
        f"placeholder.{f.value}" in ids for f in (RiskFactorKind.WAVE_HEIGHT, RiskFactorKind.WIND)
    )


def test_a_loaded_definition_replaces_only_its_own_factors_placeholder(tmp_path: Path) -> None:
    engine = build_engine(_settings(_write(tmp_path, _file(tioman()))), now=NOW)
    assert engine is not None
    ids = {getattr(r, "rule_id", "") for r in engine._rules}
    assert "definition.synthetic.wave_height" in ids
    assert "placeholder.wave_height" not in ids
    assert "placeholder.wind" in ids and "placeholder.swell" in ids


def test_the_production_engine_still_cannot_give_go_with_one_definition(tmp_path: Path) -> None:
    from tests.conftest import make_evidence

    from divesafe.domain import DataCategory, DivePlan, Recommendation

    engine = build_engine(_settings(_write(tmp_path, _file(tioman()))), now=NOW)
    assert engine is not None
    plan = DivePlan(
        site_id="site-a",
        planned_start=NOW + timedelta(hours=1),
        planned_duration_minutes=30,
        max_depth_m=10,
    )
    perfect = [make_evidence(c) for c in DataCategory]
    result = engine.assess(plan, perfect, NOW)
    assert result.recommendation == Recommendation.INSUFFICIENT_EVIDENCE
    assert RiskFactorKind.WIND in result.unevaluated_factors


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(status="REQUIRES DOMAIN VALIDATION"),
        lambda d: d.update(unit="ft"),
        lambda d: d.update(reviews=[]),
    ],
)
def test_startup_fails_loudly_if_any_definition_is_refused(tmp_path: Path, mutate: Any) -> None:
    definition = copy.deepcopy(raw())
    mutate(definition)
    with pytest.raises(RuntimeError, match="refused definitions"):
        build_engine(_settings(_write(tmp_path, _file(definition))), now=NOW)


def test_startup_fails_if_a_definition_has_expired(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="expired"):
        build_engine(_settings(_write(tmp_path, _file(raw()))), now=EXPIRES + timedelta(days=1))


def test_startup_fails_if_the_ruleset_path_is_not_a_readable_file(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="cannot be loaded"):
        build_engine(_settings(tmp_path / "missing.json"), now=NOW)


def test_the_loaded_version_is_recorded_as_the_ruleset_version(tmp_path: Path) -> None:
    engine = build_engine(
        _settings(_write(tmp_path, _file(tioman(), version="synthetic-9"))), now=NOW
    )
    assert engine is not None and engine._ruleset_version.startswith("synthetic-9@")
    assert len(engine._ruleset_version.split("@")[1]) == 12  # the content hash prefix


def test_no_ruleset_of_limits_is_shipped_in_the_repository() -> None:
    """The repository defines no limits: they come only from a separately reviewed file."""
    root = Path(__file__).resolve().parents[2]
    shipped = [
        str(p.relative_to(root))
        for p in list(root.rglob("*.json")) + list(root.rglob("*.jsonc"))
        if ".venv" not in p.parts
        and "tests" not in p.parts
        and ".git" not in p.parts
        and '"definitions"' in p.read_text(errors="ignore")
    ]
    assert shipped == []


# --- review follow-ups: parsing strictness, file reading, integrity ------------------------------


def _bytes(*definitions: dict[str, Any], version: str = "synthetic-1") -> bytes:
    return _file(*definitions, version=version).encode()


def test_duplicate_json_keys_are_rejected_not_resolved_to_the_last() -> None:
    text = _file(tioman()).replace(
        '"status": "VALIDATED"', '"status": "VALIDATED", "status": "VALIDATED"'
    )
    with pytest.raises(RulesetError, match="duplicate keys"):
        load_ruleset(text, NOW_AFTER_SIGNOFF)
    swapped = '{"ruleset_version": "v", "definitions": [], "definitions": []}'
    with pytest.raises(RulesetError):
        load_ruleset(swapped, NOW_AFTER_SIGNOFF)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_literals_are_rejected_at_parse_time(literal: str) -> None:
    text = _file(tioman()).replace('"value": 3.0', f'"value": {literal}')
    with pytest.raises(RulesetError):
        load_ruleset(text, NOW_AFTER_SIGNOFF)


def test_a_huge_integer_is_an_error_not_a_crash() -> None:
    text = _file(tioman()).replace('"value": 3.0', '"value": ' + "9" * 5000)
    with pytest.raises(RulesetError):
        load_ruleset(text, NOW_AFTER_SIGNOFF)
    loaded = load_ruleset(
        _file(tioman()).replace('"value": 3.0', '"value": 1e999'), NOW_AFTER_SIGNOFF
    )
    assert not loaded.is_clean  # infinity after parsing is refused by the model


def test_invalid_utf8_is_an_error_not_a_traceback() -> None:
    with pytest.raises(RulesetError):
        load_ruleset_bytes(b"\xff\xfe{", NOW_AFTER_SIGNOFF)


def test_the_version_label_has_a_strict_shape() -> None:
    for bad in ("", "has space", "x" * 65, "line\nbreak", "a/b"):
        with pytest.raises(RulesetError):
            load_ruleset(_file(version=bad), NOW_AFTER_SIGNOFF)


def test_the_content_hash_identifies_exactly_what_was_loaded() -> None:
    first = load_ruleset_bytes(_bytes(tioman()), NOW_AFTER_SIGNOFF)
    same = load_ruleset_bytes(_bytes(tioman()), NOW_AFTER_SIGNOFF)
    edited_limit = tioman(no_go_when={"comparison": ">", "value": 3.5})
    edited = load_ruleset_bytes(_bytes(edited_limit), NOW_AFTER_SIGNOFF)
    assert first.sha256 == same.sha256 and len(first.sha256) == 64
    assert edited.sha256 != first.sha256  # same version label, different content, different hash
    assert edited.version == first.version and edited.identity != first.identity


def test_unknown_site_ids_are_refused_so_a_typo_cannot_silently_never_apply() -> None:
    typo = tioman(scope={"site_ids": ["my-pahang-pulau-tiomn"]})
    loaded = load_ruleset(_file(typo), NOW_AFTER_SIGNOFF, known_sites=[SITE])
    assert not loaded.is_clean and "not registered" in loaded.refused[0].reason
    assert load_ruleset(_file(tioman()), NOW_AFTER_SIGNOFF, known_sites=[SITE]).is_clean


def test_a_sign_off_dated_in_the_future_is_refused() -> None:
    loaded = load_ruleset(_file(tioman()), NOW_AFTER_SIGNOFF - timedelta(days=365))
    assert not loaded.is_clean
    assert any("future" in r.reason or "expired" in r.reason for r in loaded.refused)


def test_hostile_ids_are_sanitised_and_error_output_is_bounded() -> None:
    hostile = tioman(id="evil\n\x1b[31mINJECT" + "x" * 500)
    loaded = load_ruleset(_file(hostile), NOW_AFTER_SIGNOFF)
    ident = loaded.refused[0].definition_id
    assert "\n" not in ident and "\x1b" not in ident and len(ident) <= 60
    many = [tioman(id=f"synthetic.bad-{i}", unit="ft") for i in range(40)]
    detail = load_ruleset(_file(*many), NOW_AFTER_SIGNOFF).describe_refusals()
    assert detail.count("synthetic.bad-") <= 10 and "and 30 more" in detail
    assert len(detail) < 10_000


def test_validation_messages_never_contain_the_submitted_metric_or_unit() -> None:
    secret = "SUBMITTED-SECRET-VALUE-456"
    bad = tioman(unit=secret)
    loaded = load_ruleset(_file(bad), NOW_AFTER_SIGNOFF)
    assert secret not in loaded.refused[0].reason
    unsupported = load_ruleset(
        _file(tioman(metric=secret.lower().replace("-", "_"))), NOW_AFTER_SIGNOFF
    )
    assert secret.lower().replace("-", "_") not in unsupported.refused[0].reason


def test_a_secondary_wave_metric_cannot_mark_total_wave_height_as_covered() -> None:
    loaded = load_ruleset(_file(tioman(metric="wind_wave_height")), NOW_AFTER_SIGNOFF)
    assert not loaded.is_clean and "no data source" in loaded.refused[0].reason


def test_reading_the_file_is_bounded_and_refuses_non_files(tmp_path: Path) -> None:
    with pytest.raises(RulesetError, match="regular file"):
        read_ruleset_file(tmp_path)  # a directory
    with pytest.raises(RulesetError, match="regular file"):
        read_ruleset_file(tmp_path / "missing.json")
    big = tmp_path / "big.json"
    big.write_bytes(b"x" * 1_000_001)
    with pytest.raises(RulesetError, match="too large"):
        read_ruleset_file(big)
    ok = tmp_path / "ok.json"
    ok.write_bytes(_bytes(tioman()))
    assert read_ruleset_file(ok) == _bytes(tioman())


def test_unreadable_or_corrupt_files_stop_startup_with_a_fixed_message(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(RuntimeError, match="cannot be loaded"):
        build_engine(_settings(bad), now=NOW)


def test_the_pinned_hash_must_match_or_startup_fails(tmp_path: Path) -> None:
    path = _write(tmp_path, _file(tioman()))
    digest = load_ruleset_bytes(path.read_bytes(), NOW).sha256
    ok = Settings(evidence_max_age_minutes=60, ruleset_path=path, ruleset_sha256=digest)
    assert build_engine(ok, now=NOW) is not None
    wrong = Settings(evidence_max_age_minutes=60, ruleset_path=path, ruleset_sha256="0" * 64)
    with pytest.raises(RuntimeError, match="does not match"):
        build_engine(wrong, now=NOW)
    path.write_text(
        _file(tioman(no_go_when={"comparison": ">", "value": 3.5}))
    )  # edited after review
    with pytest.raises(RuntimeError, match="does not match"):
        build_engine(ok, now=NOW)


def test_the_ruleset_pin_must_be_a_lowercase_sha256_hex_string() -> None:
    with pytest.raises(ValueError):
        Settings(ruleset_sha256="not-a-hash")
    with pytest.raises(ValueError):
        Settings(ruleset_sha256="A" * 64)


def test_a_hostile_extra_key_cannot_inject_into_the_error_text() -> None:
    bad = tioman()
    bad["evil\n\x1b[31mkey"] = 1
    loaded = load_ruleset(_file(bad), NOW_AFTER_SIGNOFF)
    reason = loaded.refused[0].reason
    assert "\n" not in reason and "\x1b" not in reason and len(reason) <= 300


def test_the_loader_requires_a_timezone_aware_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        load_ruleset(_file(), datetime(2026, 1, 1))


def test_the_pin_is_checked_before_the_file_is_parsed(tmp_path: Path) -> None:
    corrupt = tmp_path / "ruleset.json"
    corrupt.write_bytes(b"\xff\xfe not even json")
    pinned = Settings(evidence_max_age_minutes=60, ruleset_path=corrupt, ruleset_sha256="1" * 64)
    with pytest.raises(RuntimeError, match="does not match"):  # not "cannot be loaded"
        build_engine(pinned, now=NOW)


_KEYS = '{"' + "a" * 64 + '": {"actor": "ops", "roles": ["viewer", "decider"]}}'


def test_production_requires_a_pinned_ruleset(tmp_path: Path) -> None:
    path = _write(tmp_path, _file())
    with pytest.raises(ValueError, match="pinned"):
        Settings(
            environment="production",
            llm_provider="ollama",
            ruleset_path=path,
            api_key_hashes=_KEYS,  # type: ignore[arg-type]
        )
    Settings(
        environment="production",
        llm_provider="ollama",
        ruleset_path=path,
        ruleset_sha256="a" * 64,
        api_key_hashes=_KEYS,  # type: ignore[arg-type]
    )
    Settings(environment="development", ruleset_path=path)  # development may run unpinned


def test_each_covered_factor_gets_a_scope_rule_in_the_production_engine(tmp_path: Path) -> None:
    engine = build_engine(_settings(_write(tmp_path, _file(tioman()))), now=NOW)
    assert engine is not None
    ids = {getattr(r, "rule_id", "") for r in engine._rules}
    assert "definition-scope.wave_height" in ids and "definition-scope.swell" not in ids
