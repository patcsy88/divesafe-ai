"""The backup and restore scripts (ADR 0024, docs/backup-and-restore.md) touch the only copy of
the human decisions. Three layers, none of which proves the scripts work against a real server
(that was run by hand, see the ADR):

1. every non-comment line of the three scripts is pinned to a reviewed copy, so any edit fails here
   and has to be read (the copies are in tests/safety/golden_backup);
2. a scan of what the scripts must never contain, which names the intent behind the pins;
3. a run of the real scripts against a fake `docker` on PATH, which checks that each refusal exits
   non-zero before anything is written and that the happy path calls the tools it should."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.safety

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "docker"
GOLDEN = Path(__file__).resolve().parent / "golden_backup"
ALL = ("backup.sh", "restore.sh", "backup-common.sh")


def _lines(name: str) -> list[str]:
    raw = (DOCKER / name).read_bytes()
    assert b"\r" not in raw, name
    text = raw.decode("ascii")
    return [line for line in text.split("\n") if not line.lstrip().startswith("#")]


def _code(name: str) -> str:
    return "\n".join(_lines(name))


@pytest.mark.parametrize("name", ALL)
def test_every_code_line_is_the_reviewed_one(name: str) -> None:
    golden = (GOLDEN / f"{name}.txt").read_bytes().decode("ascii")
    assert _code(name) == golden, (
        f"{name} changed. Read the diff against tests/safety/golden_backup/{name}.txt, have it "
        "reviewed (security and risk), then update the copy."
    )


# --- what the scripts must never contain ---------------------------------------------------------


@pytest.mark.parametrize("name", ALL)
def test_no_script_puts_a_password_on_a_command_line_or_in_the_host_environment(name: str) -> None:
    code = _code(name)
    assert not re.search(r"--password|\s-W\b", code), name
    assert "docker exec -e" not in code and "--env" not in code, name
    assert "docker inspect" not in code and "docker logs" not in code, name
    assert "/run/secrets" not in code or name == "backup-common.sh", name
    for line in code.splitlines():
        if "PGPASSWORD" in line:
            assert re.fullmatch(
                r"  docker exec -i \"\$db\" sh -c 'PGPASSWORD=\"\$\(cat /run/secrets/"
                r"db_owner_password\)\" PGSSLMODE=require exec \"\$@\"' sh \"\$@\"",
                line,
            ), line


def test_the_owner_password_is_read_once_inside_the_database_container() -> None:
    common = _code("backup-common.sh")
    assert common.count("db_owner_password") == 1
    assert common.count("PGPASSWORD") == 1
    for name in ("backup.sh", "restore.sh"):
        assert "db_owner_password" not in _code(name), name
        assert "PGPASSWORD" not in _code(name), name


@pytest.mark.parametrize("name", ALL)
def test_no_script_uses_the_database_superuser(name: str) -> None:
    code = _code(name)
    assert "divesafe_bootstrap" not in code and "db_bootstrap" not in code, name
    assert "in_db " not in code and "in_db()" not in code, name
    for line in code.splitlines():
        for match in re.finditer(r"-U (\S+)", line):
            assert match.group(1) in ("divesafe_owner", "scratch"), (name, line)


def test_the_fact_queries_cannot_be_redirected_by_objects_in_public() -> None:
    sql = _code("backup-common.sh")
    assert "SET LOCAL search_path = pg_catalog, pg_temp;" in sql
    assert sql.index("SET LOCAL search_path") < sql.index("SELECT 'assessments='")
    # Every `name` column that reaches an aggregate or a comparison is cast to text first.
    assert "string_agg(tgname," not in sql and "string_agg(relname," not in sql
    assert "string_agg(extname," not in sql
    for needle in ("tgname::text", "relname::text", "extname::text"):
        assert needle in sql, needle
    assert "pg_catalog.pg_class" in _code("restore.sh")


@pytest.mark.parametrize("name", ALL)
def test_no_script_can_switch_the_guard_off_or_replace_existing_data(name: str) -> None:
    code = _code(name).lower()
    for word in (
        "session_replication_role",
        "disable-triggers",
        "--clean",
        "--create",
        "--if-exists",
        "superuser",
        "dropdb",
        "createuser",
        "createdb -",
    ):
        if name == "backup-common.sh" and word in ("createdb -",):
            continue  # the scratch server inside the throwaway container
        assert word not in code, (name, word)
    for pattern in (
        r"disable\s+trigger",
        r"drop\s+(trigger|table|schema|function|database|owned)",
        r"alter\s+(function|trigger|table|role|database|system)",
        r"create\s+(or\s+replace\s+)?(function|trigger|role|user)",
        r"\btruncate\b",
        r"delete\s+from",
        r"\bupdate\s+public",
        r"\binsert\s+into",
        r"pg_restore[^\n]*\s-[a-z]*[cC]\b",
        r"--role\b",
    ):
        assert not re.search(pattern, code), (name, pattern)


def test_the_archive_is_never_loaded_by_the_production_superuser() -> None:
    assert "pg_restore" not in _code("backup.sh")
    restores = [x for x in _code("restore.sh").splitlines() if "pg_restore" in x]
    assert restores == [
        "in_db_owner pg_restore -h db -U divesafe_owner -d divesafe --no-password --no-owner"
        " --no-acl \\"
    ]
    for name in ALL:
        for line in _code(name).splitlines():
            if "pg_restore" in line:
                assert "divesafe_bootstrap" not in line, (name, line)
    common = _code("backup-common.sh")
    assert common.count("pg_restore") == 1 and common.count("initdb") == 1
    assert "in_db_owner pg_restore" not in common


def test_the_scratch_server_has_no_network_no_secrets_and_no_mounts() -> None:
    common = _code("backup-common.sh")
    start = common.index("docker run")
    # Only the options of `docker run`, up to the image name; the script text after it is psql's.
    block = common[start : common.index('--entrypoint sh "$image"', start)]
    for flag in (
        "--rm",
        "--pull never",
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges:true",
        "--user 999:999",
        "--memory 4g",
        "--memory-swap 4g",
        "--cpus 2",
    ):
        assert flag in block, flag
    for forbidden in (
        "-v ",
        "--volume",
        "--mount",
        "--privileged",
        "--network host",
        "--network=host",
        "--cap-add",
        "--env-file",
        "--device",
        "secret",
        "docker.sock",
        "--pid ",
        "--pid=",
        "--ipc",
        "--add-host",
        "--link",
    ):
        assert forbidden not in block, forbidden
    assert len(re.findall(r"(?:^|\s)-e ", block)) == 1 and '-e "FACTS_SQL=' in block
    assert "-c listen_addresses= -c" in common  # no TCP listener in the scratch server


def test_the_scratch_image_is_the_database_image() -> None:
    compose = (DOCKER / "docker-compose.production.yml").read_text()
    image = re.search(r"^image=(\S+)$", _code("backup-common.sh"), re.M)
    assert image is not None
    db_block = compose[compose.index("\n  db:\n") : compose.index("\n  db-init:")]
    assert f"image: {image.group(1)}" in db_block


def test_the_only_drop_in_any_script_is_none() -> None:
    for name in ALL:
        assert "drop database" not in _code(name).lower(), name


def test_restore_refuses_before_it_writes_anything() -> None:
    code = _code("restore.sh")
    first_write = code.index("in_db_owner pg_restore")
    for needle in (
        'if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then',
        "the api is running",
        'if [ "$existing" != "0" ]; then',
        'if [ "$trial" != "$want" ]; then',
        "if ! shape_ok",
        "if ! facts_complete",
    ):
        assert code.index(needle) < first_write, needle
    assert code.count("in_db_owner pg_restore") == 1
    # every refusal block exits non-zero
    for block in re.findall(r"then\n(.*?)\n\s*fi\n", code, re.S):
        if "refusing" in block or "does NOT match" in block:
            assert "exit 1" in block, block


def test_the_scripts_are_executable_and_stop_on_error() -> None:
    for name in ("backup.sh", "restore.sh"):
        assert os.access(DOCKER / name, os.X_OK), name
        assert "set -eu" in _code(name), name
        assert (DOCKER / name).read_text().startswith("#!/bin/sh\n"), name


def test_archives_partial_files_and_manifests_cannot_be_committed_or_sent_to_a_build() -> None:
    for ignore in (ROOT / ".gitignore", ROOT / ".dockerignore"):
        lines = [x.strip() for x in ignore.read_text().splitlines()]
        assert not [x for x in lines if x.startswith("!") and ("dump" in x or "manifest" in x)]
        for pattern in ("*.dump", "*.dump.part", "divesafe-*.manifest", "*.manifest.part"):
            assert any(
                x.lstrip("*/") == pattern.lstrip("*/") or x == pattern or x.endswith("/" + pattern)
                for x in lines
            ), (ignore.name, pattern)


# --- the real scripts against a fake docker ------------------------------------------------------

FACTS = (
    "assessments=2\n"
    "dbsettings=0\n"
    "decided=1\n"
    "digest=aaaa\n"
    "extensions=plpgsql,vector\n"
    "failure_counts=0\n"
    "guard_defs=bbbb\n"
    "guards=assessments_append_only_guard,assessments_insert_guard,assessments_no_truncate_guard\n"
    "hardening=0\n"
    "migrations=1:cc,2:dd\n"
    "objects=eeee\n"
    "pending_runs=0\n"
    "tables=assessments:r,pending_runs:r,run_failure_counts:r,schema_migrations:r\n"
    "server=16.0\n"
)

FAKE_DOCKER = r"""#!/bin/sh
# Records what the scripts ask Docker to do and answers from files named in the environment.
case "$1" in
  ps)
    case "$*" in
      *com.docker.compose.project=*) ;;
      *label=com.docker.compose.service=db*)
        case "$*" in
          *--format*) echo "divesafe-db-1 divesafe-prod"; echo "old-db-1 divesafe-old"; exit 0 ;;
        esac
        if [ "${FAKE_TWO_DBS:-0}" = 1 ]; then printf 'dbid\nolddb\n'; else echo dbid; fi
        exit 0
        ;;
    esac
    for arg in "$@"; do
      case "$arg" in
        label=com.docker.compose.service=api) [ "${FAKE_API_UP:-0}" = 1 ] && echo apiid || true ;;
        label=com.docker.compose.service=db) echo dbid ;;
      esac
    done
    ;;
  run)
    echo "run" >>"$FAKE_LOG"
    cat >/dev/null
    cat "$FAKE_SCRATCH_FACTS"
    if [ "${FAKE_SCRATCH_FAIL:-0}" = 1 ]; then exit 1; fi
    ;;
  exec)
    shift 7
    echo "exec $*" >>"$FAKE_LOG"
    input="$(cat)"
    case "$1" in
      pg_dump) printf 'DUMPDATA' ;;
      pg_restore) printf '%s' "$input" >/dev/null ;;
      psql)
        case "$input" in
          *"FROM pg_catalog.pg_class c"*) echo "${FAKE_EXISTING:-0}" ;;
          *"BEGIN READ ONLY"*)
            if [ "${FAKE_REPORT_FAIL:-0}" = 1 ]; then exit 1; fi
            echo "latest assessment created (UTC): 2026-10-18T10:00:00Z"
            echo "runs whose outcome is unknown (marked running in the backup), before the api:"
            echo "run-1 2026-10-18T09:59:00Z"
            ;;
          *"BEGIN ISOLATION"*)
            if [ "${FAKE_LIVE_UNSTABLE:-0}" = 1 ]; then
              n="$(cat "$FAKE_LIVE_FACTS.n" 2>/dev/null || echo 0)"
              echo "$((n + 1))" >"$FAKE_LIVE_FACTS.n"
              sed "s/^digest=.*/digest=changed$n/" "$FAKE_LIVE_FACTS"
            else
              cat "$FAKE_LIVE_FACTS"
            fi
            if [ "${FAKE_LIVE_FAIL:-0}" = 1 ]; then exit 1; fi
            ;;
        esac
        ;;
    esac
    ;;
  *) echo "unexpected docker $*" >&2; exit 99 ;;
esac
"""


class Harness:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "docker"
        fake.write_text(FAKE_DOCKER)
        fake.chmod(0o755)
        self.log = tmp / "docker.log"
        self.log.write_text("")
        self.live = tmp / "live.facts"
        self.scratch = tmp / "scratch.facts"
        self.live.write_text(FACTS)
        self.scratch.write_text(FACTS)
        self.out = tmp / "backups"
        self.out.mkdir(mode=0o700)
        self.out.chmod(0o700)
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE_LOG": str(self.log),
            "FAKE_LIVE_FACTS": str(self.live),
            "FAKE_SCRATCH_FACTS": str(self.scratch),
            "HOME": str(tmp),
        }

    def run(self, script: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(DOCKER / script), *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            env={**self.env, **env},
            timeout=60,
            check=False,
        )

    def calls(self) -> str:
        return self.log.read_text()

    def backup(self) -> tuple[Path, Path]:
        result = self.run("backup.sh", str(self.out))
        assert result.returncode == 0, result.stderr
        dump = next(self.out.glob("*.dump"))
        return dump, dump.with_suffix(".manifest")


@pytest.fixture()
def h() -> Iterator[Harness]:
    with tempfile.TemporaryDirectory() as raw:
        yield Harness(Path(raw))


def test_backup_writes_a_private_archive_and_a_manifest_of_the_live_facts(h: Harness) -> None:
    dump, manifest = h.backup()
    assert dump.read_bytes() == b"DUMPDATA"
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    text = manifest.read_text()
    assert f"dump_sha256={hashlib.sha256(b'DUMPDATA').hexdigest()}" in text
    for line in FACTS.splitlines():
        if not line.startswith("server="):
            assert line in text.splitlines(), line
    assert not list(h.out.glob("*.part")) and not (h.out / ".backup.lock").exists()
    assert "pg_restore" not in h.calls()


def test_backup_keeps_nothing_when_the_scratch_restore_differs(h: Harness) -> None:
    h.scratch.write_text(FACTS.replace("digest=aaaa", "digest=ffff"))
    result = h.run("backup.sh", str(h.out))
    assert result.returncode != 0 and "does NOT match" in result.stderr
    assert list(h.out.iterdir()) == []


def test_backup_keeps_nothing_when_a_guard_trigger_is_missing(h: Harness) -> None:
    short = FACTS.replace(",assessments_no_truncate_guard", "")
    h.live.write_text(short)
    h.scratch.write_text(short)
    result = h.run("backup.sh", str(h.out))
    assert result.returncode != 0 and "guard trigger is missing" in result.stderr
    assert list(h.out.iterdir()) == []


def test_backup_keeps_nothing_when_the_facts_cannot_be_read(h: Harness) -> None:
    h.live.write_text("")
    result = h.run("backup.sh", str(h.out))
    assert result.returncode != 0
    assert list(h.out.iterdir()) == []


def test_backup_refuses_a_directory_others_can_enter_or_one_inside_the_repository(
    h: Harness,
) -> None:
    open_dir = h.tmp / "open"
    open_dir.mkdir()
    open_dir.chmod(0o755)
    result = h.run("backup.sh", str(open_dir))
    assert result.returncode != 0 and "0700" in result.stderr and list(open_dir.iterdir()) == []
    inside = Path(tempfile.mkdtemp(dir=ROOT, prefix="backup-test-"))
    try:
        inside.chmod(0o700)
        result = h.run("backup.sh", str(inside))
        assert result.returncode != 0 and "inside the repository" in result.stderr
        assert list(inside.iterdir()) == []
    finally:
        shutil.rmtree(inside)


def test_backup_refuses_while_another_one_holds_the_lock(h: Harness) -> None:
    (h.out / ".backup.lock").mkdir()
    result = h.run("backup.sh", str(h.out))
    assert result.returncode != 0 and "another backup" in result.stderr
    assert not list(h.out.glob("*.dump"))


def _restore_inputs(h: Harness) -> Path:
    dump, _ = h.backup()
    h.log.write_text("")
    return dump


def test_restore_loads_as_the_owner_after_every_check(h: Harness) -> None:
    dump = _restore_inputs(h)
    result = h.run("restore.sh", str(dump))
    assert result.returncode == 0, result.stderr
    calls = h.calls().splitlines()
    restores = [x for x in calls if x.startswith("exec pg_restore")]
    assert len(restores) == 1
    assert "-U divesafe_owner" in restores[0] and "-U divesafe_bootstrap" not in restores[0]
    assert "--no-acl" in restores[0] and "--single-transaction" in restores[0]
    assert "--exit-on-error" in restores[0]
    assert calls.index("run") < calls.index(restores[0])


@pytest.mark.parametrize(
    "case",
    [
        "api_up",
        "not_empty",
        "archive_changed",
        "manifest_missing",
        "scratch_differs",
        "manifest_without_guards",
        "after_restore_differs",
        "manifest_wrong_format",
    ],
)
def test_restore_refuses_and_nothing_is_written(h: Harness, case: str) -> None:
    dump = _restore_inputs(h)
    manifest = dump.with_suffix(".manifest")
    env: dict[str, str] = {}
    if case == "api_up":
        env["FAKE_API_UP"] = "1"
    elif case == "not_empty":
        env["FAKE_EXISTING"] = "3"
    elif case == "archive_changed":
        dump.write_bytes(dump.read_bytes() + b"x")
    elif case == "manifest_missing":
        manifest.unlink()
    elif case == "scratch_differs":
        h.scratch.write_text(FACTS.replace("assessments=2", "assessments=1"))
    elif case == "manifest_without_guards":
        manifest.write_text(
            "\n".join(x for x in manifest.read_text().splitlines() if not x.startswith("guards="))
            + "\n"
        )
    elif case == "manifest_wrong_format":
        manifest.write_text(manifest.read_text().replace("format=1", "format=2"))
    elif case == "after_restore_differs":
        # The scratch server agrees with the manifest, the real database does not.
        h.live.write_text(FACTS.replace("decided=1", "decided=0"))
        result = h.run("restore.sh", str(dump), **env)
        assert result.returncode != 0 and "Do not start the api" in result.stderr
        return
    result = h.run("restore.sh", str(dump), **env)
    assert result.returncode != 0, result.stderr
    assert "pg_restore" not in h.calls(), case


def test_restore_check_compares_without_writing(h: Harness) -> None:
    dump = _restore_inputs(h)
    ok = h.run("restore.sh", "--check", str(dump), FAKE_API_UP="1")
    assert ok.returncode == 0 and "matches the manifest" in ok.stderr
    h.live.write_text(FACTS.replace("digest=aaaa", "digest=ffff"))
    bad = h.run("restore.sh", "--check", str(dump))
    assert bad.returncode != 0 and "does NOT match" in bad.stderr
    assert "pg_restore" not in h.calls() and "run" not in h.calls().splitlines()


def test_a_project_name_that_could_be_an_option_is_refused(h: Harness) -> None:
    result = h.run("backup.sh", str(h.out), DIVESAFE_COMPOSE_PROJECT="--bad")
    assert result.returncode != 0 and "DIVESAFE_COMPOSE_PROJECT" in result.stderr


def test_backup_gives_up_when_the_database_keeps_changing_during_the_dump(h: Harness) -> None:
    result = h.run("backup.sh", str(h.out), FAKE_LIVE_UNSTABLE="1")
    assert result.returncode != 0 and "kept changing" in result.stderr
    assert list(h.out.iterdir()) == []
    assert h.calls().count("exec pg_dump") == 3


def test_restore_hashes_and_loads_the_same_private_copy() -> None:
    code = _code("restore.sh")
    assert code.count('cp "$dump" "$work/archive.dump"') == 1
    # After the copy the original path is never read again.
    copy = 'cp "$dump" "$work/archive.dump"'
    after_copy = code[code.index(copy) + len(copy) :]
    assert '"$dump"' not in after_copy and "$dump " not in after_copy
    # The manifest is read from a private copy as well, never from its original path again.
    assert code.count('cp "$manifest" "$work/manifest"') == 1
    after_manifest = code[code.index('cp "$manifest" "$work/manifest"') + 30 :]
    assert '"$manifest"' not in after_manifest
    assert 'sha256_of "$work/archive.dump"' in code
    assert 'read_scratch "$work/archive.dump"' in code
    assert '<"$work/archive.dump"' in code


@pytest.mark.parametrize("name", ALL)
def test_no_script_turns_off_error_stopping_or_swallows_a_failure_silently(name: str) -> None:
    code = _code(name)
    assert "set +e" not in code and "set +u" not in code, name
    assert "|| :" not in code and "2>/dev/null || true" not in code.replace(
        'rmdir "$lock" 2>/dev/null || true', ""
    ), name


def test_the_facts_are_what_the_manifest_comparison_depends_on() -> None:
    sql = _code("backup-common.sh")
    # Every fact the comparison lists is produced, and nothing is only counted that must be named.
    for fact in (
        "assessments",
        "dbsettings",
        "decided",
        "digest",
        "extensions",
        "failure_counts",
        "guard_defs",
        "guards",
        "hardening",
        "migrations",
        "objects",
        "pending_runs",
        "tables",
    ):
        assert f"SELECT '{fact}='" in sql, fact
        assert fact in sql.split("compared_facts()")[1].split("}")[0], fact
    assert "string_agg(tgname::text, ','" in sql  # trigger names, not a count
    assert "pg_get_functiondef" in sql and "pg_get_triggerdef" in sql
    assert 'ORDER BY id COLLATE "C"' in sql and "YYYY-MM-DD" in sql
    assert "AT TIME ZONE 'UTC'" in sql and "record::text" in sql
    assert "ISOLATION LEVEL REPEATABLE READ READ ONLY" in sql
    assert "SET LOCAL row_security = off;" in sql  # a policy makes the query fail, not filter
    hardening = sql[sql.index("SELECT 'hardening='") : sql.index("SELECT 'objects='")]
    for part in ("pg_policy", "relforcerowsecurity", "pg_default_acl", "pg_db_role_setting"):
        assert part in hardening, part  # the `dbsettings` fact alone does not count role settings
    assert hardening.count("+ (SELECT count(*)") == 6
    assert (
        "FROM pg_namespace" in hardening and "NOT IN ('public', 'information_schema')" in hardening
    )
    assert "nspname::text NOT LIKE 'pg\\_%')" in hardening  # system schemas are the only exception
    assert "rolsuper" in sql and "'_RETURN'" in sql


def test_a_fact_that_cannot_be_read_is_never_compared_as_equal(h: Harness) -> None:
    # The same failure on both sides must not pass: a missing fact stops the backup.
    short = "".join(x for x in FACTS.splitlines(keepends=True) if not x.startswith("guard_defs="))
    h.live.write_text(short)
    h.scratch.write_text(short)
    result = h.run("backup.sh", str(h.out))
    assert result.returncode != 0 and list(h.out.iterdir()) == []


def test_a_table_that_is_not_a_table_stops_the_backup(h: Harness) -> None:
    changed = FACTS.replace("run_failure_counts:r", "run_failure_counts:v")
    h.live.write_text(changed)
    h.scratch.write_text(changed)
    result = h.run("backup.sh", str(h.out))
    assert result.returncode != 0 and list(h.out.iterdir()) == []


def test_two_databases_running_stop_both_scripts(h: Harness) -> None:
    dump = _restore_inputs(h)
    for args in (("backup.sh", str(h.out)), ("restore.sh", str(dump))):
        result = h.run(*args, FAKE_TWO_DBS="1")
        assert result.returncode != 0 and "database containers" in result.stderr, args
    assert "pg_restore" not in h.calls()


def test_the_scripts_say_which_project_and_volume_they_act_on(h: Harness) -> None:
    result = h.run("backup.sh", str(h.out), DIVESAFE_COMPOSE_PROJECT="my-restored")
    assert "project 'my-restored' (volume my-restored_db-data)" in result.stderr
    dump = next(h.out.glob("*.dump"))
    refused = h.run(
        "restore.sh", str(dump), DIVESAFE_COMPOSE_PROJECT="my-restored", FAKE_EXISTING="3"
    )
    assert refused.returncode != 0
    assert "do NOT remove its volume (my-restored_db-data)" in refused.stderr


def test_a_restore_lists_the_runs_whose_outcome_is_unknown(h: Harness) -> None:
    dump = _restore_inputs(h)
    result = h.run("restore.sh", str(dump))
    assert result.returncode == 0, result.stderr
    assert "runs whose outcome is unknown" in result.stderr
    assert "latest assessment created (UTC): 2026-10-18T10:00:00Z" in result.stderr
    broken = h.run("restore.sh", str(dump), FAKE_REPORT_FAIL="1")
    assert broken.returncode != 0 and "Do not start the api" in broken.stderr
    code = _code("restore.sh")
    assert "WHERE state = 'RUNNING'" in code
    # Listed before the api is told it may start, and after the comparison succeeded.
    assert code.index("does NOT match the manifest (or its facts") < code.index(
        "WHERE state = 'RUNNING'"
    )


def test_a_database_level_setting_stops_the_backup_and_the_restore(h: Harness) -> None:
    changed = FACTS.replace("dbsettings=0", "dbsettings=1")
    h.live.write_text(changed)
    h.scratch.write_text(changed)
    result = h.run("backup.sh", str(h.out))
    assert result.returncode != 0 and list(h.out.iterdir()) == []


def test_the_backup_directory_is_checked_with_find_not_the_text_of_ls() -> None:
    code = _code("backup.sh")
    assert 'find "$dir" -maxdepth 0 -type d -perm 0700' in code
    assert "unset CDPATH" in code and "unset CDPATH" in _code("restore.sh")
    assert 'dir="$(cd "$dir" && pwd -P)"' in code


def test_a_failing_read_is_not_accepted_even_when_it_printed_every_fact(h: Harness) -> None:
    live = h.run("backup.sh", str(h.out), FAKE_LIVE_FAIL="1")
    assert live.returncode != 0 and list(h.out.iterdir()) == []
    scratch = h.run("backup.sh", str(h.out), FAKE_SCRATCH_FAIL="1")
    assert scratch.returncode != 0 and "does NOT match" in scratch.stderr
    assert list(h.out.iterdir()) == []
    dump = _restore_inputs(h)
    result = h.run("restore.sh", str(dump), FAKE_SCRATCH_FAIL="1")
    assert result.returncode != 0 and "pg_restore" not in h.calls()
    # The read after the load fails: the load happened, and the script must say not to start.
    result = h.run("restore.sh", str(dump), FAKE_LIVE_FAIL="1")
    assert result.returncode != 0 and "Do not start the api" in result.stderr


def test_small_details_of_the_scratch_container_and_the_directory_check_are_pinned() -> None:
    common = _code("backup-common.sh")
    assert "--tmpfs /tmp:rw,size=2g,uid=999,gid=999,mode=0700" in common
    assert "string_agg(relname::text || ':' || relkind::text, ','" in common
    backup = _code("backup.sh")
    assert 'case "$mode" in *+) mine="" ;; esac' in backup  # an ACL is refused
    assert backup.index('case "$mode" in *+)') < backup.index('if [ -z "$mine" ]; then')


@pytest.mark.parametrize("bad", ["hardening=1", "hardening=2"])
def test_a_policy_default_privilege_rule_stored_setting_or_superuser_owner_is_refused(
    h: Harness, bad: str
) -> None:
    changed = FACTS.replace("hardening=0", bad)
    h.live.write_text(changed)
    h.scratch.write_text(changed)
    result = h.run("backup.sh", str(h.out))
    assert result.returncode != 0 and list(h.out.iterdir()) == []


def test_a_manifest_that_lists_hardening_above_zero_is_refused_by_restore(h: Harness) -> None:
    dump = _restore_inputs(h)
    manifest = dump.with_suffix(".manifest")
    manifest.write_text(manifest.read_text().replace("hardening=0", "hardening=1"))
    result = h.run("restore.sh", str(dump))
    assert result.returncode != 0 and "pg_restore" not in h.calls()


def test_every_compose_database_counts_whatever_its_image_and_project() -> None:
    common = _code("backup-common.sh")
    body = common[common.index("single_database()") : common.index("in_db_owner()")]
    assert "ancestor" not in common
    assert "--filter label=com.docker.compose.service=db" in body
    assert "com.docker.compose.project" in body  # each match is listed with its project


def test_the_acl_check_counts_the_entries_that_macos_ls_prints_after_the_mode() -> None:
    backup = _code("backup.sh")
    assert 'ls -lde "$dir"' in backup
    assert 'case "$(uname -s)" in Darwin) [ "$aces" -eq 1 ] || mine="" ;; esac' in backup
    assert backup.index('[ "$aces" -eq 1 ]') < backup.index('if [ -z "$mine" ]; then')


def test_what_the_restore_prints_from_the_archive_is_stripped_to_printable_text() -> None:
    code = _code("restore.sh")
    assert "LC_ALL=C tr -cd '[:print:]\\n'" in code
    report = code[code.index('report="$(') : code.index("tr -cd")]
    assert "'latest assessment created (UTC): '" in report and "max(created_at)" in report
    assert "SET LOCAL search_path = pg_catalog, pg_temp;" in report
    assert "BEGIN READ ONLY;" in report
