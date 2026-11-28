# ruff: noqa: E501
"""What Caddy forwards to web (ADR 0017 Amendment 2026-11-24), run against the real docker/Caddyfile
in the pinned caddy image, in scratch containers on scratch internal networks with the file's own
fixed subnets (10.231.79-81.0/24). web is replaced by a tiny echo server at web's address, so the
test sees exactly the X-Forwarded-For and X-Forwarded-Host that reach web.

Needs Docker and the two images, both pinned by digest: caddy from the compose file, node here
(NODE_IMAGE, one line, so CI can extract it). The test itself never pulls (`--pull never`); CI pulls
both pinned images before the full suite and fails if this file skips (owner #149b; the
extraction is pinned by tests/unit/test_ci_image_extraction.py). Without Docker or an image, as
on a developer machine, it skips with SKIP_PREFIX. It is marked `integration`, not `safety`: the
offline checks of the same rules are tests/safety/test_caddyfile.py, which never needs Docker.

Every container and network is named `zz-caddyt-<random>`, nothing is published, the only mount
is the Caddyfile read-only, and all of it is removed afterwards, also when a test fails or a
client run times out (cleanup removes every container with the run's prefix). If one of the
subnets is already in use (for example by a deployed divesafe stack on this host), Docker refuses
the network and the test skips."""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
CADDYFILE = ROOT / "docker" / "Caddyfile"
COMPOSE = ROOT / "docker" / "docker-compose.production.yml"
# One line, ending in the closing quote, because CI extracts it with sed (owner #149b); hence
# the file-level E501 exemption at the top, which nothing else in this file needs.
# fmt: off
NODE_IMAGE = "node:26-alpine@sha256:8996bb5821d62a8fd9bb27c1cb07462f3fdce6e47d640c6a881ead0b064ce216"
# fmt: on
SKIP_PREFIX = "caddy forwarding cannot run:"

LAN_NAME = "lan.example.home.arpa"
PUBLIC_NAME = "public.example.org"
# Scratch networks with the Caddyfile's own subnets; all internal (the test needs no route out).
NETWORKS = {
    "ingress": ("10.231.81.0/24", "10.231.81.128/25"),
    "edge": ("10.231.80.0/24", "10.231.80.128/25"),
    "proxy": ("10.231.79.0/24", "10.231.79.128/25"),
}
CADDY_IPS = {"ingress": "10.231.81.10", "edge": "10.231.80.10", "proxy": "10.231.79.10"}
WEB_IP = "10.231.79.20"
LAN_CLIENT = "10.231.81.50"
NEWT = "10.231.80.20"
OTHER_EDGE_PEER = "10.231.80.30"
# Documentation addresses only (RFC 5737).
FORGED = "198.51.100.7"
PANGOLIN_SEEN = "203.0.113.9"

LOCKED = [
    "--read-only",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges:true",
    "--memory",
    "256m",
    "--pids-limit",
    "128",
    "--pull",
    "never",
]

ECHO = (
    'require("http").createServer((q,r)=>{console.log("REQ "+(q.headers.host??""));'
    'r.setHeader("content-type","application/json");'
    'r.end(JSON.stringify({xff:q.headers["x-forwarded-for"]??null,'
    'xfh:q.headers["x-forwarded-host"]??null,host:q.headers.host??null}))})'
    '.listen(3100,"0.0.0.0")'
)
CLIENT = (
    'const s=process.env.S==="1";const m=require(s?"https":"http");'
    'const q=m.request({host:process.env.IP,port:Number(process.env.P),path:"/",'
    "servername:process.env.SNI,rejectUnauthorized:false,"
    'headers:JSON.parse(process.env.H)},r=>{let b="";r.on("data",c=>b+=c);'
    'r.on("end",()=>console.log(JSON.stringify({status:r.statusCode,body:b})))});'
    'q.on("error",e=>console.log(JSON.stringify({error:e.code||"error"})));'
    'q.setTimeout(5000,()=>q.destroy(Object.assign(new Error(),{code:"TIMEOUT"})));q.end();'
)


def _skip(reason: str) -> NoReturn:
    pytest.skip(f"{SKIP_PREFIX} {reason}")


def _docker(*args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=timeout
    )


def _caddy_image() -> str:
    match = re.search(r"^\s+image: (caddy:\S+@sha256:[0-9a-f]{64})\s*$", COMPOSE.read_text(), re.M)
    assert match, "the compose file pins no caddy image"
    return match.group(1)


class Rig:
    def __init__(self) -> None:
        self.caddy_image = ""
        self.prefix = f"zz-caddyt-{uuid.uuid4().hex[:8]}"
        self.networks: dict[str, str] = {}
        self.containers: list[str] = []

    def net(self, role: str) -> str:
        return self.networks[role]

    def request(
        self,
        role: str,
        source_ip: str,
        *,
        port: int,
        tls: bool,
        target: str,
        headers: Any,
        sni: str | None = LAN_NAME,
    ) -> dict[str, Any]:
        """One request from a scratch client at `source_ip` on network `role`."""
        result = _docker(
            "run",
            "--rm",
            "--name",
            f"{self.prefix}-client-{uuid.uuid4().hex[:6]}",
            "--network",
            self.net(role),
            "--ip",
            source_ip,
            "--user",
            "1000:1000",
            *LOCKED,
            "-e",
            f"IP={target}",
            "-e",
            f"P={port}",
            "-e",
            f"S={'1' if tls else '0'}",
            "-e",
            f"SNI={(sni or '') if tls else ''}",
            "-e",
            f"H={json.dumps(headers)}",
            NODE_IMAGE,
            "node",
            "-e",
            CLIENT,
        )
        return dict(json.loads(result.stdout.strip().splitlines()[-1]))

    def cleanup(self) -> None:
        # Every container of this run, the --rm clients included (a timed-out client is left).
        listed = _docker(
            "ps", "-aq", "--filter", f"name=^{self.prefix}-", check=False, timeout=30
        ).stdout.split()
        for name in self.containers:
            assert name.startswith("zz-")
            _docker("rm", "-f", name, check=False)
        for container in listed:
            _docker("rm", "-f", container, check=False)
        for name in self.networks.values():
            assert name.startswith("zz-")
            _docker("network", "rm", name, check=False)


# A test-only Caddyfile is passed in the environment and written to the container's /tmp, so
# the only host mount is still the real Caddyfile, read-only.
COPY_COMMAND = (
    'printf "%s" "$DIVESAFE_TEST_CADDYFILE" > /tmp/Caddyfile && '
    "exec caddy run --config /tmp/Caddyfile --adapter caddyfile"
)


def _start_caddy(rig: Rig, name: str, text: str | None = None) -> None:
    """caddy at its three fixed addresses with the real Caddyfile, or with `text` instead."""
    rig.containers.append(name)
    source = (
        ["--mount", f"type=bind,source={CADDYFILE},target=/etc/caddy/Caddyfile,readonly"]
        if text is None
        else ["-e", f"DIVESAFE_TEST_CADDYFILE={text}"]
    )
    command = [] if text is None else ["sh", "-c", COPY_COMMAND]
    _docker(
        "create", "--name", name, "--network", rig.net("ingress"), "--ip",
        CADDY_IPS["ingress"], "--user", "65534:65534", *LOCKED,
        "--tmpfs", "/tmp:size=16m,noexec,nosuid,nodev",
        "--tmpfs", "/data:size=16m,mode=1777", "--tmpfs", "/config:size=4m,mode=1777",
        "-e", f"DIVESAFE_WEB_LAN_NAME={LAN_NAME}",
        "-e", f"DIVESAFE_WEB_PUBLIC_NAME={PUBLIC_NAME}",
        *source, rig.caddy_image, *command,
    )  # fmt: skip
    for role in ("edge", "proxy"):
        _docker("network", "connect", "--ip", CADDY_IPS[role], rig.net(role), name)
    _docker("start", name)
    _wait_listening(name)


def _wait_listening(name: str) -> None:
    probe = f"nc -z -w 2 {CADDY_IPS['ingress']} 8443 && nc -z -w 2 {CADDY_IPS['edge']} 8080"
    for _ in range(40):
        if _docker("exec", name, "sh", "-c", probe, check=False).returncode == 0:
            return
        time.sleep(0.5)
    logs = _docker("logs", name, check=False).stderr[-2000:]
    pytest.fail(f"caddy did not start listening: {logs}")


@contextlib.contextmanager
def _caddy_with(rig: Rig, text: str) -> Iterator[None]:
    """The real caddy stopped (freeing its fixed addresses), a test-only one in its place, and
    the real one back and listening afterwards, whatever happens in between."""
    real = rig.containers[1]
    variant = f"{rig.prefix}-caddy-variant-{uuid.uuid4().hex[:6]}"
    _docker("stop", "-t", "5", real)
    # BaseException: pytest.fail raises an OutcomeException, which is not an Exception.
    original: BaseException | None = None
    try:
        _start_caddy(rig, variant, text)
        yield
    except BaseException as exc:
        original = exc
        raise
    finally:
        _docker("rm", "-f", variant, check=False)
        try:
            _docker("start", real)
            _wait_listening(real)
        except BaseException as restart:
            if original is None:
                raise
            # Both failed: report both, and keep the test's own failure as the cause.
            raise RuntimeError(
                f"restarting the real caddy failed ({restart!r}) after the block failed "
                f"({original!r})"
            ) from original


@pytest.fixture(scope="module")
def rig() -> Iterator[Rig]:
    if shutil.which("docker") is None:
        _skip("docker is not available")
    try:
        _docker("info", timeout=20)
    except (subprocess.SubprocessError, OSError):
        _skip("the docker daemon does not answer")
    caddy_image = _caddy_image()
    for image in (caddy_image, NODE_IMAGE):
        if _docker("image", "inspect", image, check=False).returncode != 0:
            _skip(f"image {image} is not present locally (never pulled here)")
    rig = Rig()
    try:
        for role, (subnet, pool) in NETWORKS.items():
            name = f"{rig.prefix}-{role}"
            made = _docker(
                "network",
                "create",
                "--internal",
                "--subnet",
                subnet,
                "--ip-range",
                pool,
                name,
                check=False,
            )
            if made.returncode != 0:
                _skip(f"cannot create a scratch network on {subnet} (already in use?)")
            rig.networks[role] = name
        web = f"{rig.prefix}-web"
        rig.containers.append(web)
        _docker(
            "run",
            "-d",
            "--name",
            web,
            "--network",
            rig.net("proxy"),
            "--ip",
            WEB_IP,
            "--user",
            "1000:1000",
            *LOCKED,
            NODE_IMAGE,
            "node",
            "-e",
            ECHO,
        )
        rig.caddy_image = caddy_image
        _start_caddy(rig, f"{rig.prefix}-caddy")
        yield rig
    finally:
        rig.cleanup()


def _seen(response: dict[str, Any]) -> dict[str, Any]:
    assert response.get("status") == 200, response
    return dict(json.loads(response["body"]))


def test_a_forged_x_forwarded_for_on_the_lan_site_reaches_web_as_the_clients_own_address(
    rig: Rig,
) -> None:
    for forged in ([FORGED, PANGOLIN_SEEN], f"{FORGED}, {PANGOLIN_SEEN}", FORGED):
        seen = _seen(
            rig.request(
                "ingress",
                LAN_CLIENT,
                port=8443,
                tls=True,
                target=CADDY_IPS["ingress"],
                headers={"Host": LAN_NAME, "X-Forwarded-For": forged},
            )
        )
        assert seen["xff"] == LAN_CLIENT, forged


def test_on_the_public_site_from_newt_web_sees_the_right_most_untrusted_address(
    rig: Rig,
) -> None:
    for chain in (
        f"{FORGED}, {PANGOLIN_SEEN}",
        f"{FORGED}, {PANGOLIN_SEEN}, {NEWT}",
        [FORGED, PANGOLIN_SEEN],
    ):
        seen = _seen(
            rig.request(
                "edge",
                NEWT,
                port=8080,
                tls=False,
                target=CADDY_IPS["edge"],
                headers={"Host": PUBLIC_NAME, "X-Forwarded-For": chain},
            )
        )
        assert seen["xff"] == PANGOLIN_SEEN, chain
    # No header from Newt: the client is Newt itself.
    seen = _seen(
        rig.request(
            "edge", NEWT, port=8080, tls=False, target=CADDY_IPS["edge"],
            headers={"Host": PUBLIC_NAME},
        )
    )  # fmt: skip
    assert seen["xff"] == NEWT


def test_on_the_public_site_a_peer_that_is_not_newt_is_seen_as_itself(rig: Rig) -> None:
    seen = _seen(
        rig.request(
            "edge",
            OTHER_EDGE_PEER,
            port=8080,
            tls=False,
            target=CADDY_IPS["edge"],
            headers={"Host": PUBLIC_NAME, "X-Forwarded-For": f"{FORGED}, {PANGOLIN_SEEN}"},
        )
    )
    assert seen["xff"] == OTHER_EDGE_PEER


def test_a_forged_x_forwarded_host_reaches_web_as_the_site_name(rig: Rig) -> None:
    for role, ip, port, tls, name, target in (
        ("ingress", LAN_CLIENT, 8443, True, LAN_NAME, CADDY_IPS["ingress"]),
        ("edge", NEWT, 8080, False, PUBLIC_NAME, CADDY_IPS["edge"]),
    ):
        for forged in ("evil.example", "127.0.0.1:3100", f"{name}:8443"):
            seen = _seen(
                rig.request(
                    role,
                    ip,
                    port=port,
                    tls=tls,
                    target=target,
                    headers={"Host": name, "X-Forwarded-Host": forged},
                )
            )
            assert seen["xfh"] == name, (role, forged)
            assert seen["host"].split(":")[0] == name, (role, forged)


def test_an_unknown_host_on_the_public_listener_gets_no_response(rig: Rig) -> None:
    for host in ("evil.example", CADDY_IPS["edge"], LAN_NAME, f"{PUBLIC_NAME}.evil.example"):
        response = rig.request(
            "edge", NEWT, port=8080, tls=False, target=CADDY_IPS["edge"], headers={"Host": host}
        )
        assert "status" not in response, (host, response)
        assert response.get("error") in ("ECONNRESET", "UND_ERR_SOCKET", "EPIPE"), response


def test_the_adapted_config_matches_what_the_offline_test_reads(rig: Rig) -> None:
    """`caddy adapt` of the same file: admin off, the two listeners, trusted proxies only on the
    public one, and no other upstream."""
    caddy = rig.containers[1]
    adapted = json.loads(
        _docker("exec", caddy, "caddy", "adapt", "--config", "/etc/caddy/Caddyfile").stdout
    )
    assert adapted["admin"]["disabled"] is True
    servers = adapted["apps"]["http"]["servers"]
    assert {k: v["listen"] for k, v in servers.items()} == {
        "lan": [f"{CADDY_IPS['ingress']}:8443"],
        "public": [f"{CADDY_IPS['edge']}:8080"],
    }
    assert "trusted_proxies" not in servers["lan"]
    # The Host must equal the SNI name on the LAN listener; never on the plain-http public one.
    assert servers["lan"]["strict_sni_host"] is True
    assert "strict_sni_host" not in servers["public"]
    assert servers["public"]["trusted_proxies"] == {"ranges": [f"{NEWT}/32"], "source": "static"}
    assert servers["public"]["trusted_proxies_strict"] == 1
    dials = re.findall(r'"dial":\s*"([^"]+)"', json.dumps(adapted))
    assert dials and set(dials) == {f"{WEB_IP}:3100"}
    assert "log_credentials" not in json.dumps(adapted)


def _adapted(rig: Rig) -> dict[str, Any]:
    caddy = rig.containers[1]
    out = _docker("exec", caddy, "caddy", "adapt", "--config", "/etc/caddy/Caddyfile").stdout
    return dict(json.loads(out))


def test_each_site_logs_to_its_own_file_kept_for_14_days_as_adapted(rig: Rig) -> None:
    """Security review S1: the 14-day access-log retention as the pinned image adapts it, not as
    text. `roll_keep_for 336h` adapts to `roll_keep_days: 14`, `roll_keep -1` stays -1 (no count
    limit, so age alone decides), `roll_at 00:00` to `["00:00"]`. Each named site's server maps its
    host to its own log, so a site without a `log` (or a longer retention) fails here."""
    adapted = _adapted(rig)
    logs = adapted["logging"]["logs"]
    sites = {"lan": LAN_NAME, "public": PUBLIC_NAME}
    servers = adapted["apps"]["http"]["servers"]
    # server -> host -> [log name]; exactly one log per named site and nothing else.
    mapped = {k: v.get("logs", {}).get("logger_names") for k, v in servers.items()}
    assert set(mapped) == set(sites)
    access: dict[str, str] = {}
    for server, host in sites.items():
        assert mapped[server] is not None and set(mapped[server]) == {host}, (server, mapped)
        (name,) = mapped[server][host]
        access[server] = name
    assert len(set(access.values())) == 2
    # The default log takes none of the access logs (no copy to stderr with another retention).
    assert set(logs) == {"default", *access.values()}
    assert sorted(logs["default"]["exclude"]) == sorted(
        f"http.log.access.{n}" for n in access.values()
    )
    assert set(logs["default"]) == {"exclude"}
    for server, name in access.items():
        assert logs[name] == {
            "writer": {
                "output": "file",
                "filename": f"/data/caddy/logs/access-{server}.log",
                "roll_at": ["00:00"],
                "roll_keep": -1,
                "roll_keep_days": 14,
            },
            "encoder": {"format": "json"},
            "include": [f"http.log.access.{name}"],
        }, (server, logs[name])
        filename = logs[name]["writer"]["filename"]
        assert Path(filename).parent == Path("/data/caddy/logs"), filename


def _reached_web(rig: Rig) -> int:
    """How many requests the echo server (web's stand-in) has received so far."""
    logs = _docker("logs", rig.containers[0], check=False).stdout
    return sum(1 for line in logs.splitlines() if line.startswith("REQ "))


def _lan(rig: Rig, sni: str, host: str) -> dict[str, Any]:
    return rig.request(
        "ingress", LAN_CLIENT, port=8443, tls=True, target=CADDY_IPS["ingress"],
        headers={"Host": host}, sni=sni,
    )  # fmt: skip


def _refused_before_web(rig: Rig, label: str, sni: str, host: str) -> dict[str, Any]:
    """The request does not reach the echo server, and no echo body comes back."""
    before = _reached_web(rig)
    response = _lan(rig, sni, host)
    assert _reached_web(rig) == before, (label, response)
    assert "xff" not in response.get("body", ""), (label, response)
    return response


def test_the_lan_listener_refuses_a_wrong_sni_or_a_foreign_host(rig: Rig) -> None:
    """Risk review: the LAN listener has no refusal site. A wrong SNI fails the TLS handshake
    whatever the Host. The right SNI with a foreign Host completes TLS and gets Caddy's 421
    (`strict_sni_host on`), and the echo server, web's stand-in, sees nothing. A Host with the
    port, as browsers send it on a non-default port, still reaches web."""
    for host in (LAN_NAME, f"{LAN_NAME}:8443"):
        before = _reached_web(rig)
        ok = _lan(rig, LAN_NAME, host)
        assert ok.get("status") == 200 and json.loads(ok["body"])["host"] == host, ok
        assert _reached_web(rig) == before + 1
    for host in (LAN_NAME, "evil.example", PUBLIC_NAME, CADDY_IPS["ingress"]):
        for sni in ("evil.example", PUBLIC_NAME):
            response = _refused_before_web(rig, f"SNI {sni}", sni, host)
            assert "status" not in response and response.get("error"), (sni, host, response)
    for host in ("evil.example", PUBLIC_NAME, CADDY_IPS["ingress"], f"{PUBLIC_NAME}:8443"):
        response = _refused_before_web(rig, "right SNI", LAN_NAME, host)
        assert response == {"status": 421, "body": ""}, (host, response)


def test_the_lan_listener_never_lets_a_request_with_no_sni_reach_web(rig: Rig) -> None:
    """No SNI at all (servername "", so Node sends none; left undefined, Node would send the Host
    as SNI). With the real file the handshake fails; either way no echo answer comes back and
    nothing reaches web, whatever the Host."""
    for host in (LAN_NAME, f"{LAN_NAME}:8443", "evil.example", CADDY_IPS["ingress"]):
        response = _refused_before_web(rig, "no SNI", "", host)
        assert response.get("status") != 200, (host, response)
        assert response.get("status") in (None, 421), (host, response)


def _with_default_sni() -> str:
    """The real Caddyfile plus `default_sni <LAN name>`, so a handshake with no SNI succeeds and
    strict_sni_host itself must refuse the request (test-only; never in docker/)."""
    text = CADDYFILE.read_text(encoding="utf-8")
    anchor = "\tauto_https disable_redirects\n"
    assert text.count(anchor) == 1
    return text.replace(anchor, anchor + "\tdefault_sni {$DIVESAFE_WEB_LAN_NAME}\n")


def test_with_no_sni_and_a_completed_handshake_caddy_answers_421_and_web_sees_nothing(
    rig: Rig,
) -> None:
    with _caddy_with(rig, _with_default_sni()):
        # The copy really completes the handshake with no SNI: the foreign-Host 421 proves it.
        for host in (LAN_NAME, f"{LAN_NAME}:8443", "evil.example", CADDY_IPS["ingress"]):
            response = _refused_before_web(rig, "no SNI, default_sni", "", host)
            assert response == {"status": 421, "body": ""}, (host, response)
        # The right name still works through the copy, so the 421s are not a broken copy.
        before = _reached_web(rig)
        assert _lan(rig, LAN_NAME, LAN_NAME).get("status") == 200
        assert _reached_web(rig) == before + 1
