"""A7 hand run (2026-11-11): `pg_restore --no-owner --no-acl` (docker/restore.sh) drops migration
4's REVOKE EXECUTE ... FROM PUBLIC on the five SECURITY DEFINER knowledge guard functions, and the
knowledge check that db-init runs then refuses. This replays the round trip on the real server:
schema on a scratch database, pg_dump, pg_restore --no-owner --no-acl into a second, empty scratch
database, the check fails; the statements restore.sh runs (read from the script itself) fix it, and
the script's own read-back accepts only then. It also compares what PUBLIC may do on every object
in `public` before and after, which is the survey behind the fix.

pg_dump and pg_restore are run with `docker exec` inside the one container whose host port binding
is the port of DIVESAFE_TEST_DATABASE_URL (they are not on the host); a `divesafe-prod` container is
never chosen, and anything but exactly one candidate skips. It restores as the test server's
superuser, not divesafe_owner: the ACL loss under test does not depend on the restoring role.
Both scratch databases are named *_test and force-dropped; the shared database is never dropped."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

import psycopg
import pytest
from tests.unit.test_repository_contract import TEST_DSN as DSN

from divesafe.services.migrations import knowledge_problems
from divesafe.services.postgres import install_schema

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DSN is None, reason="DIVESAFE_TEST_DATABASE_URL not set"),
]

RESTORE = Path(__file__).parents[2] / "docker" / "restore.sh"
GUARDS = (
    "kb_chunks_insert",
    "kb_documents_insert",
    "kb_no_truncate",
    "kb_no_update_delete",
    "kb_status_events_insert",
)


def _dsn_for(database: str) -> str:
    assert DSN is not None
    parts = psycopg.conninfo.conninfo_to_dict(DSN)
    parts["dbname"] = database
    return psycopg.conninfo.make_conninfo(**parts)  # type: ignore[arg-type]


LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
_LOOPBACK_BINDINGS = ("127.0.0.1", "::1")
_ALL_INTERFACES = ("", "0.0.0.0", "::")
_PROTECTED = "divesafe-prod"
# CI greps the pytest log for this prefix (.github/workflows/ci.yml): there the test must run.
SKIP_PREFIX = "restore round trip cannot run:"
# Only the name, the compose project label and the port bindings: no container env is read.
_INSPECT_FORMAT = (
    '{"name":{{json .Name}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"ports":{{json .NetworkSettings.Ports}}}'
)


def _skip(reason: str) -> NoReturn:
    pytest.skip(f"{SKIP_PREFIX} {reason}")


def _docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=True, timeout=30
    ).stdout


def _container_for_port(host: str, port: str, inspected: list[dict[str, Any]]) -> list[str]:
    """Names of the containers whose HOST side of a port binding is `port`, bound on loopback, or
    on all interfaces (CI's service binds 0.0.0.0 and ::). Nothing unless the DSN host is loopback,
    so a binding seen here is the server the DSN reaches. A container named or labelled as a
    `divesafe-prod` compose project never counts."""
    if host not in LOOPBACK_HOSTS:
        return []
    found: list[str] = []
    for c in inspected:
        name = str(c.get("name") or "").lstrip("/")
        project = str(c.get("project") or "")
        if name.startswith(_PROTECTED) or project.startswith(_PROTECTED):
            continue
        ports = c.get("ports") or {}
        if any(
            str(b.get("HostPort", "")) == port
            and str(b.get("HostIp", "")) in _LOOPBACK_BINDINGS + _ALL_INTERFACES
            for bindings in ports.values()
            for b in (bindings or [])
        ):
            found.append(name)
    return found


def _container() -> str:
    """The test database container, by its host port binding (`docker inspect`), never by
    `docker ps --filter publish=`, which can match the container-side port. Skips (with
    SKIP_PREFIX, which fails CI) unless the DSN host is loopback and exactly one container
    qualifies."""
    assert DSN is not None
    parts = psycopg.conninfo.conninfo_to_dict(DSN)
    host, port = str(parts.get("host") or ""), str(parts.get("port") or "5432")
    if host not in LOOPBACK_HOSTS:
        _skip("the DSN host is not loopback")
    if shutil.which("docker") is None:
        _skip("docker is not available to run pg_dump / pg_restore")
    try:
        ids = _docker("ps", "-q").split()
        lines = _docker("inspect", "--format", _INSPECT_FORMAT, *ids).splitlines() if ids else []
        inspected = [json.loads(line) for line in lines if line.strip()]
    except (subprocess.SubprocessError, OSError, ValueError):
        _skip("could not list the running containers")
    return _only(_container_for_port(host, port, inspected))


def _only(found: list[str]) -> str:
    if len(found) != 1:
        _skip("not exactly one non-production container binds the DSN's port on the host")
    return found[0]


def _in_container(container: str, *args: str, data: bytes = b"") -> bytes:
    return subprocess.run(
        ["docker", "exec", "-i", container, *args],
        input=data,
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout


@pytest.fixture
def scratch() -> Iterator[tuple[str, str]]:
    assert DSN is not None
    name = str(psycopg.conninfo.conninfo_to_dict(DSN).get("dbname", ""))
    if not name.endswith("_test"):
        raise RuntimeError("DIVESAFE_TEST_DATABASE_URL must point at a database named *_test")
    tag = uuid.uuid4().hex[:10]
    source, target = f"divesafe_aclsrc_{tag}_test", f"divesafe_acldst_{tag}_test"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{source}"')  # type: ignore[call-overload]
        conn.execute(f'CREATE DATABASE "{target}"')  # type: ignore[call-overload]
    try:
        yield source, target
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            for database in (source, target):
                conn.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')  # type: ignore[call-overload]


def _restore_sql() -> tuple[str, str]:
    """(the function revokes, the read-back query) exactly as restore.sh sends them."""
    code = RESTORE.read_text()
    revokes = re.findall(
        r'"(REVOKE EXECUTE ON FUNCTION public\.kb_[a-z_]+\(\) FROM PUBLIC;)"', code
    )
    assert len(revokes) == len(GUARDS)
    check = re.search(r'"(SELECT count\(\*\) FILTER .*?aclexplode\(p\.proacl\).*?;)"', code)
    assert check is not None
    return "\n".join(revokes), check.group(1)


def _public_may(conn: psycopg.Connection[Any]) -> set[tuple[str, str]]:
    """Every (object, privilege) PUBLIC holds in schema public, plus on the schema itself."""
    rows = conn.execute(
        "SELECT 'function ' || p.proname, 'EXECUTE' FROM pg_proc p "
        "WHERE p.pronamespace = 'public'::regnamespace "
        "AND has_function_privilege('public', p.oid, 'EXECUTE') "
        "UNION ALL SELECT 'relation ' || c.relname, x.priv FROM pg_class c, "
        "unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) "
        "x(priv) WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r','p','v') "
        "AND has_table_privilege('public', c.oid, x.priv) "
        "UNION ALL SELECT 'sequence ' || c.relname, x.priv FROM pg_class c, "
        "unnest(ARRAY['USAGE','SELECT','UPDATE']) x(priv) "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'S' "
        "AND has_sequence_privilege('public', c.oid, x.priv) "
        "UNION ALL SELECT 'schema public', x.priv FROM unnest(ARRAY['USAGE','CREATE']) x(priv) "
        "WHERE has_schema_privilege('public', 'public', x.priv)"
    ).fetchall()
    return {(str(r[0]), str(r[1])) for r in rows}


def _execute_problems(dsn: str) -> list[str]:
    with psycopg.connect(dsn) as conn:
        return [p for p in knowledge_problems(conn, owner_is_current_user=True) if "EXECUTE" in p]


def test_a_no_acl_restore_opens_the_guards_and_the_restore_sql_closes_them(
    scratch: tuple[str, str],
) -> None:
    source, target = scratch
    container = _container()
    install_schema(_dsn_for(source))
    assert _execute_problems(_dsn_for(source)) == []
    with psycopg.connect(_dsn_for(source)) as conn:
        before = _public_may(conn)

    archive = _in_container(container, "pg_dump", "-U", "postgres", "-Fc", "-d", source)
    _in_container(
        container,
        "pg_restore",
        "-U",
        "postgres",
        "-d",
        target,
        "--no-owner",
        "--no-acl",
        "--single-transaction",
        "--exit-on-error",
        data=archive,
    )
    revokes, check = _restore_sql()
    dst = _dsn_for(target)

    # The defect: every guard reported, as in the A7 hand run, and the script's read-back refuses.
    assert _execute_problems(dst) == [
        f"a role other than the owner may EXECUTE {name}" for name in GUARDS
    ]
    with psycopg.connect(dst) as conn:
        assert conn.execute(check).fetchone() == ("0 5",)
        opened = _public_may(conn) - before
    assert opened == {(f"function {name}", "EXECUTE") for name in GUARDS}

    # The fix, as restore.sh runs it.
    with psycopg.connect(dst, autocommit=True) as conn:
        conn.execute(revokes)  # type: ignore[call-overload]
    assert _execute_problems(dst) == []
    with psycopg.connect(dst) as conn:
        assert conn.execute(check).fetchone() == ("5 5",)
        # Nothing else PUBLIC may do differs from the database the schema was made in.
        assert _public_may(conn) == before


def test_the_container_is_chosen_by_host_port_and_never_a_production_one() -> None:
    """Offline: the selection rule alone, on data shaped like `_INSPECT_FORMAT`'s output."""

    def c(name: str, port: str, ip: str = "127.0.0.1", project: str = "") -> dict[str, Any]:
        return {
            "name": f"/{name}",
            "project": project,
            "ports": {"5432/tcp": [{"HostIp": ip, "HostPort": port}], "9/tcp": None},
        }

    lo = "127.0.0.1"
    assert _container_for_port(lo, "55432", [c("pg16", "55432"), c("pg17", "55433")]) == ["pg16"]
    assert _container_for_port("::1", "55432", [c("pg16", "55432", ip="::1")]) == ["pg16"]
    # The container-side port 5432 is not a host binding.
    assert _container_for_port(lo, "5432", [c("pg16", "55432")]) == []
    # Another server on host port 5432 is not the one the DSN's 55432 reaches.
    assert _container_for_port(lo, "55432", [c("other", "5432"), c("pg16", "55432")]) == ["pg16"]
    # CI: all interfaces, IPv4 and IPv6, with a loopback DSN host.
    for ip in ("0.0.0.0", "::", ""):
        assert _container_for_port("localhost", "5432", [c("svc", "5432", ip=ip)]) == ["svc"]
        assert _container_for_port(lo, "5432", [c("svc", "5432", ip=ip)]) == ["svc"]
    both = {
        "name": "/svc",
        "project": None,
        "ports": {
            "5432/tcp": [
                {"HostIp": "0.0.0.0", "HostPort": "5432"},
                {"HostIp": "::", "HostPort": "5432"},
            ]
        },
    }
    assert _container_for_port("localhost", "5432", [both]) == ["svc"]
    # A non-loopback DSN host: nothing, whatever the binding.
    for host in ("db.example", "10.0.0.5", "", "/var/run/postgresql"):
        for ip in ("0.0.0.0", "::", "", "127.0.0.1"):
            assert _container_for_port(host, "5432", [c("svc", "5432", ip=ip)]) == []
    # A binding on another address is not the loopback server.
    assert _container_for_port(lo, "55432", [c("pg16", "55432", ip="192.168.1.9")]) == []
    assert _container_for_port(lo, "55432", [c("divesafe-prod-db-1", "55432")]) == []
    assert _container_for_port(lo, "55432", [c("db-1", "55432", project="divesafe-prod")]) == []
    assert _container_for_port(lo, "55432", [c("a", "55432"), c("b", "55432")]) == ["a", "b"]
    assert _only(["pg16"]) == "pg16"
    for found in ([], ["a", "b"]):
        with pytest.raises(pytest.skip.Exception, match=re.escape(SKIP_PREFIX)):
            _only(found)


def test_ci_fails_when_this_round_trip_is_skipped() -> None:
    """Review K1: a skip here must not pass unnoticed in CI, and the URL guard stays as it was."""
    ci = (Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml").read_text()
    assert "run: \"! grep -q 'DIVESAFE_TEST_DATABASE_URL not set' pytest.log\"" in ci
    assert f"run: \"! grep -q '{SKIP_PREFIX}' pytest.log\"" in ci
    assert "run: pytest -rs | tee pytest.log" in ci
    assert "DIVESAFE_TEST_DATABASE_URL: postgresql://postgres:ci-only-password@localhost:" in ci
    assert ".Env" not in _INSPECT_FORMAT
    assert set(re.findall(r"\{\{json ([^}]*)\}\}", _INSPECT_FORMAT)) == {
        ".Name",
        '(index .Config.Labels "com.docker.compose.project")',
        ".NetworkSettings.Ports",
    }
