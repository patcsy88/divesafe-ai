"""docker/Caddyfile (ADR 0017 Amendment 2026-11-24, ADR 0024 Amendment 2): Caddy is web's only way
in, so what it forwards is what web and the API believe. A small offline parser, because Caddy is
not a dependency: it reads the file by lines and braces, refuses the Caddyfile features it does
not handle (quotes, heredocs, line continuations, file imports), and expands the one snippet, so a
comment can never satisfy a check and the checks see what each site actually runs.

The adapted JSON (`caddy adapt` in a scratch container) is cross-checked in
tests/integration/test_caddy_forwarding_live.py, together with the forwarding behaviour."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytestmark = pytest.mark.safety

CADDYFILE = Path(__file__).resolve().parents[2] / "docker" / "Caddyfile"

LAN_ADDRESS = "10.231.81.10"  # caddy on web-ingress (the published LAN port)
EDGE_ADDRESS = "10.231.80.10"  # caddy on pangolin-edge (the public site, for Newt)
NEWT = "10.231.80.20/32"  # the one peer the public listener trusts
UPSTREAM = "10.231.79.20:3100"  # web on web-proxy, never by name

LAN_SITE = "https://{$DIVESAFE_WEB_LAN_NAME}:8443"
PUBLIC_SITE = "http://{$DIVESAFE_WEB_PUBLIC_NAME}:8080"
REFUSAL_SITE = "http://:8080"

TIMEOUTS = [
    ("timeouts",),
    ("read_header", "10s"),
    ("read_body", "30s"),
    ("write", "60s"),
    ("idle", "2m"),
]


@dataclass
class Node:
    tokens: tuple[str, ...]
    children: list[Node] | None = None
    line: int = 0

    @property
    def name(self) -> str:
        return self.tokens[0] if self.tokens else ""


@dataclass
class Parsed:
    globals: list[Node] = field(default_factory=list)
    snippets: dict[str, list[Node]] = field(default_factory=dict)
    sites: dict[str, list[Node]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _lines(text: str) -> list[tuple[int, list[str]]]:
    """Whitespace-split tokens per line, with comments dropped. In a Caddyfile `#` starts a
    comment only at the start of a token. Features this parser does not model are refused."""
    out: list[tuple[int, list[str]]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        tokens: list[str] = []
        for token in raw.split():
            if token.startswith("#"):
                break
            tokens.append(token)
        if not tokens:
            continue
        joined = " ".join(tokens)
        if any(c in joined for c in '"`\\') or "<<" in joined:
            raise ValueError(f"line {number}: quotes, heredocs or continuations are not handled")
        out.append((number, tokens))
    return out


def _tree(text: str) -> list[Node]:
    root: list[Node] = []
    stack: list[list[Node]] = [root]
    for number, tokens in _lines(text):
        if tokens == ["}"]:
            if len(stack) == 1:
                raise ValueError(f"line {number}: unbalanced close brace")
            stack.pop()
            continue
        if "{" in tokens[:-1] or "}" in tokens:
            raise ValueError(f"line {number}: a brace not at the end of its line")
        if tokens[-1] == "{":
            node = Node(tuple(tokens[:-1]), [], number)
            stack[-1].append(node)
            assert node.children is not None
            stack.append(node.children)
        else:
            stack[-1].append(Node(tuple(tokens), None, number))
    if len(stack) != 1:
        raise ValueError("unbalanced braces")
    return root


def _expand(nodes: list[Node], snippets: dict[str, list[Node]], problems: list[str]) -> list[Node]:
    """Inline `import <snippet> <args...>`, substituting {args[N]}. Any other import is refused."""
    out: list[Node] = []
    for node in nodes:
        if node.name == "import":
            if len(node.tokens) < 2 or node.tokens[1] not in snippets or node.children is not None:
                problems.append(f"line {node.line}: import of something that is not a snippet")
                continue
            args = node.tokens[2:]
            out.extend(_substitute(n, args) for n in snippets[node.tokens[1]])
            continue
        children = None if node.children is None else _expand(node.children, snippets, problems)
        out.append(Node(node.tokens, children, node.line))
    return out


def _substitute(node: Node, args: tuple[str, ...]) -> Node:
    def sub(token: str) -> str:
        return re.sub(r"\{args\[(\d+)\]\}", lambda m: args[int(m.group(1))], token)

    children = None if node.children is None else [_substitute(c, args) for c in node.children]
    return Node(tuple(sub(t) for t in node.tokens), children, node.line)


def parse(text: str) -> Parsed:
    parsed = Parsed()
    nodes = _tree(text)
    raw_sites: list[Node] = []
    for index, node in enumerate(nodes):
        if node.children is None:
            parsed.problems.append(f"line {node.line}: a top-level line outside any block")
        elif node.tokens == ():
            if index != 0:
                parsed.problems.append("the global options block is not first")
            parsed.globals = node.children
        elif len(node.tokens) == 1 and re.fullmatch(r"\([a-z_]+\)", node.tokens[0]):
            parsed.snippets[node.tokens[0][1:-1]] = node.children
        else:
            raw_sites.append(node)
    for node in raw_sites:
        if len(node.tokens) != 1:
            parsed.problems.append(f"line {node.line}: a site block with several addresses")
        if node.tokens[0] in parsed.sites:
            parsed.problems.append(f"line {node.line}: a site address repeated")
        assert node.children is not None
        parsed.sites[node.tokens[0]] = _expand(node.children, parsed.snippets, parsed.problems)
    return parsed


def _flat(node: Node) -> list[tuple[str, ...]]:
    """A block as a list of token tuples, depth first, children after their parent."""
    out = [node.tokens]
    for child in node.children or []:
        out.extend(_flat(child))
    return out


def _all_nodes(nodes: list[Node]) -> list[Node]:
    out: list[Node] = []
    for node in nodes:
        out.append(node)
        out.extend(_all_nodes(node.children or []))
    return out


def _servers(parsed: Parsed) -> dict[str, list[tuple[str, ...]]]:
    out: dict[str, list[tuple[str, ...]]] = {}
    for node in parsed.globals:
        if node.name == "servers":
            key = " ".join(node.tokens[1:])
            out[key] = [t for c in node.children or [] for t in _flat(c)]
    return out


def _named_site(listen: str, tls: bool, log: str) -> list[tuple[str, ...]]:
    return [
        ("bind", listen),
        *([("tls", "internal")] if tls else []),
        ("request_body",),
        ("max_size", "1MB"),
        ("log",),
        ("output", "file", f"/data/caddy/logs/access-{log}.log"),
        ("roll_at", "00:00"),
        ("roll_keep", "-1"),
        ("roll_keep_for", "336h"),
        ("format", "json"),
        ("reverse_proxy", UPSTREAM),
        ("header_up", "X-Forwarded-For", "{client_ip}"),
        ("header_up", "X-Forwarded-Host", "{host}"),
    ]


def caddyfile_problems(text: str) -> list[str]:
    """Every check on the Caddyfile; empty when it is exactly the intended layout."""
    try:
        parsed = parse(text)
    except ValueError as error:
        return [str(error)]
    problems = list(parsed.problems)
    every = [*parsed.globals, *(n for s in parsed.snippets.values() for n in s)]
    every += [n for site in parsed.sites.values() for n in site]
    all_nodes = _all_nodes(every)
    all_tokens = [t for n in all_nodes for t in n.tokens]

    # Global options: exactly these, admin off first among them.
    plain = [n.tokens for n in parsed.globals if n.name != "servers"]
    if plain != [
        ("admin", "off"),
        ("persist_config", "off"),
        ("skip_install_trust",),
        ("http_port", "8080"),
        ("https_port", "8443"),
        ("auto_https", "disable_redirects"),
    ]:
        problems.append(f"global options {plain}")
    if any(n.name == "admin" and n.children is not None for n in parsed.globals):
        problems.append("admin has a block")
    # The two listeners: the LAN one trusts nobody, the public one trusts Newt only, strictly.
    servers = _servers(parsed)
    if set(servers) != {f"{LAN_ADDRESS}:8443", f"{EDGE_ADDRESS}:8080"}:
        problems.append(f"servers {sorted(servers)}")
    lan = servers.get(f"{LAN_ADDRESS}:8443", [])
    public = servers.get(f"{EDGE_ADDRESS}:8080", [])
    if lan != [("name", "lan"), ("protocols", "h1", "h2"), ("strict_sni_host", "on"), *TIMEOUTS]:
        problems.append(f"lan server {lan}")
    if public != [
        ("name", "public"),
        ("trusted_proxies", "static", NEWT),
        ("trusted_proxies_strict",),
        *TIMEOUTS,
    ]:
        problems.append(f"public server {public}")
    # The Host must equal the SNI name on the LAN listener only. The public one is plain http,
    # with no SNI to compare, so the option has no place there and is pinned absent; a second
    # copy, or another value (`insecure_off`), is refused too.
    strict = [n.tokens for n in all_nodes if n.name == "strict_sni_host"]
    if strict != [("strict_sni_host", "on")] or ("strict_sni_host", "on") not in lan:
        problems.append(f"strict_sni_host {strict} is not exactly `on` on the LAN server")
    trusting = [n for n in all_nodes if n.name.startswith(("trusted_proxies", "client_ip_headers"))]
    if [n.tokens for n in trusting] != [
        ("trusted_proxies", "static", NEWT),
        ("trusted_proxies_strict",),
    ]:
        problems.append("a trusted proxy or client IP header outside the public server")

    # Exactly the two named sites and the refusal site, by address.
    if set(parsed.sites) != {LAN_SITE, PUBLIC_SITE, REFUSAL_SITE}:
        problems.append(f"sites {sorted(parsed.sites)}")
    if set(parsed.snippets) != {"to_web"}:
        problems.append(f"snippets {sorted(parsed.snippets)}")
    expected = {
        LAN_SITE: _named_site(LAN_ADDRESS, True, "lan"),
        PUBLIC_SITE: _named_site(EDGE_ADDRESS, False, "public"),
        REFUSAL_SITE: [("bind", EDGE_ADDRESS), ("abort",)],
    }
    for address, want in expected.items():
        got = [t for n in parsed.sites.get(address, []) for t in _flat(n)]
        if got != want:
            problems.append(f"site {address}: {got}")

    # Belt and braces over the whole file, whatever the layout.
    proxies = [n for n in all_nodes if n.name == "reverse_proxy"]
    for node in proxies:
        if node.tokens != ("reverse_proxy", UPSTREAM) or [
            c.tokens for c in node.children or []
        ] != [
            ("header_up", "X-Forwarded-For", "{client_ip}"),
            ("header_up", "X-Forwarded-Host", "{host}"),
        ]:
            problems.append(f"line {node.line}: reverse_proxy {node.tokens}")
    refusal = [n for n in _all_nodes(parsed.sites.get(REFUSAL_SITE, []))]
    if any(
        n.name in ("reverse_proxy", "import", "respond", "redir", "file_server") for n in refusal
    ):
        problems.append("the refusal site does more than abort")
    if "log_credentials" in all_tokens:
        problems.append("log_credentials")
    if any(n.name == "header" for n in all_nodes):
        problems.append("a header directive (web sends Strict-Transport-Security)")
    if any("strict-transport-security" in t.lower() for t in all_tokens):
        problems.append("Strict-Transport-Security")
    for token in all_tokens:
        # An env placeholder with a default ({$X:y}) would make an empty name a catch-all.
        if re.search(r"\{\$[A-Z_]+:", token):
            problems.append(f"an env placeholder with a default: {token}")
    return problems


def _text() -> str:
    return CADDYFILE.read_text(encoding="utf-8")


def test_the_caddyfile_is_exactly_the_intended_layout() -> None:
    assert caddyfile_problems(_text()) == []


def test_the_parser_ignores_comments_and_sees_the_snippet_in_each_site() -> None:
    parsed = parse(_text())
    assert parsed.problems == []
    tokens = [
        t for n in _all_nodes([*parsed.globals, *parsed.snippets["to_web"]]) for t in n.tokens
    ]
    assert not any(t.startswith("#") for t in tokens)
    for site in (LAN_SITE, PUBLIC_SITE):
        assert [n.name for n in parsed.sites[site]][-3:] == ["request_body", "log", "reverse_proxy"]


def test_every_reverse_proxy_overwrites_both_forwarded_headers_and_dials_web() -> None:
    parsed = parse(_text())
    proxies = [n for s in parsed.sites.values() for n in _all_nodes(s) if n.name == "reverse_proxy"]
    assert len(proxies) == 2
    for node in proxies:
        assert node.tokens == ("reverse_proxy", UPSTREAM)
        children = [c.tokens for c in node.children or []]
        assert ("header_up", "X-Forwarded-For", "{client_ip}") in children
        assert ("header_up", "X-Forwarded-Host", "{host}") in children


def test_the_refusal_site_only_aborts_on_the_public_listener() -> None:
    sites = parse(_text()).sites
    assert [n.tokens for n in sites[REFUSAL_SITE]] == [("bind", EDGE_ADDRESS), ("abort",)]
    assert all(n.children is None for n in sites[REFUSAL_SITE])


def _moved_to_public() -> tuple[str, str]:
    """strict_sni_host taken off the LAN server and put on the public one, as one replacement of
    the text between the two (read from the file, so the comments in between stay)."""
    text = CADDYFILE.read_text(encoding="utf-8")
    start = text.find("\t\tstrict_sni_host on\n")
    end = text.find("\t\ttrusted_proxies_strict\n") + len("\t\ttrusted_proxies_strict\n")
    if start < 0 or end < start:
        # Not in the file as expected: this one case then fails its count check, rather than
        # the whole module failing to collect.
        return "strict_sni_host on, then trusted_proxies_strict: not found", ""
    old = text[start:end]
    new = old.replace("\t\tstrict_sni_host on\n", "", 1) + "\t\tstrict_sni_host on\n"
    return old, new


# Each mutation must be caught. `old` must occur exactly once in the real file.
MUTATIONS = {
    "admin on": ("admin off", "admin localhost:2019"),
    "admin dropped": ("\tadmin off\n", "\n"),
    "LAN trusts a proxy": (
        "\t\tname lan\n",
        "\t\tname lan\n\t\ttrusted_proxies static private_ranges\n",
    ),
    "public trusts more": (f"static {NEWT}", "static 10.231.80.0/24"),
    "public trusts private ranges": (f"static {NEWT}", "static private_ranges"),
    "public not strict": ("\t\ttrusted_proxies_strict\n", ""),
    "a client IP header": (
        "\t\ttrusted_proxies_strict\n",
        "\t\ttrusted_proxies_strict\n\t\tclient_ip_headers X-Real-IP\n",
    ),
    "XFF passed through": ("\t\theader_up X-Forwarded-For {client_ip}\n", ""),
    "XFF from the remote host": ("X-Forwarded-For {client_ip}", "X-Forwarded-For {remote_host}"),
    "XFF appended": (
        "header_up X-Forwarded-For {client_ip}",
        "header_up +X-Forwarded-For {client_ip}",
    ),
    "XFH passed through": ("\t\theader_up X-Forwarded-Host {host}\n", ""),
    "XFH from the request": ("X-Forwarded-Host {host}", "X-Forwarded-Host {hostport}"),
    "upstream by name": (f"reverse_proxy {UPSTREAM}", "reverse_proxy web:3100"),
    "a second upstream": (
        f"reverse_proxy {UPSTREAM}",
        f"reverse_proxy {UPSTREAM} 10.231.79.21:3100",
    ),
    "no body limit": ("\t\tmax_size 1MB\n", ""),
    "a larger body limit": ("max_size 1MB", "max_size 100MB"),
    "credentials logged": (
        "\t\tformat json\n",
        "\t\tformat json\n\t}\n\tlog {\n\t\tlog_credentials\n",
    ),
    "HSTS from caddy": (
        "\timport to_web lan\n",
        "\timport to_web lan\n\theader Strict-Transport-Security max-age=31536000\n",
    ),
    "log kept longer": ("roll_keep_for 336h", "roll_keep_for 8760h"),
    "log elsewhere": ("/data/caddy/logs/", "/var/log/"),
    "log as console": ("format json", "format console"),
    "a lan timeout dropped": (
        "\t\tstrict_sni_host on\n\t\ttimeouts {\n\t\t\tread_header 10s\n",
        "\t\tstrict_sni_host on\n\t\ttimeouts {\n",
    ),
    "strict_sni_host removed": ("\t\tstrict_sni_host on\n", ""),
    "strict_sni_host off": ("strict_sni_host on", "strict_sni_host off"),
    "strict_sni_host insecure_off": ("strict_sni_host on", "strict_sni_host insecure_off"),
    "strict_sni_host commented out": ("\t\tstrict_sni_host on\n", "\t\t# strict_sni_host on\n"),
    "strict_sni_host moved to public": _moved_to_public(),
    "strict_sni_host added on public": (
        "\t\ttrusted_proxies_strict\n",
        "\t\ttrusted_proxies_strict\n\t\tstrict_sni_host on\n",
    ),
    "HTTP/3 on": ("protocols h1 h2", "protocols h1 h2 h3"),
    "refusal proxies": (
        "\tbind 10.231.80.10\n\tabort\n",
        f"\tbind 10.231.80.10\n\treverse_proxy {UPSTREAM}\n",
    ),
    "refusal responds": ("\tabort\n", "\trespond 404\n"),
    "refusal site dropped": ("http://:8080 {\n\tbind 10.231.80.10\n\tabort\n}\n", ""),
    "a catch-all site": ("http://:8080 {", ":8080 {"),
    "a third named site": (
        "http://:8080 {",
        "http://other.example.org:8080 {\n\tbind 10.231.80.10\n\timport to_web x\n}\n\n"
        "http://:8080 {",
    ),
    "a name default": (
        "{$DIVESAFE_WEB_PUBLIC_NAME}",
        "{$DIVESAFE_WEB_PUBLIC_NAME:public.example.org}",
    ),
    "LAN site on the edge": ("\tbind 10.231.81.10\n", "\tbind 10.231.80.10\n"),
    "public site on all addresses": (
        "\tbind 10.231.80.10\n\timport to_web public\n",
        "\timport to_web public\n",
    ),
    "public site gets tls": (
        "\timport to_web public\n",
        "\ttls internal\n\timport to_web public\n",
    ),
    "a file import": ("\timport to_web lan\n", "\timport to_web lan\n\timport /etc/caddy/extra\n"),
    "redirects on": ("auto_https disable_redirects", "auto_https off"),
    "a quoted token": ("admin off", 'admin "off"'),
}


@pytest.mark.parametrize(("old", "new"), list(MUTATIONS.values()), ids=list(MUTATIONS))
def test_mutation_each_weakening_of_the_caddyfile_is_caught(old: str, new: str) -> None:
    text = _text()
    assert text.count(old) == 1, old
    assert caddyfile_problems(text.replace(old, new)) != []


def test_a_comment_never_satisfies_a_check() -> None:
    text = _text()
    old = "\t\theader_up X-Forwarded-For {client_ip}\n"
    assert text.count(old) == 1
    mutated = text.replace(old, "\t\t# header_up X-Forwarded-For {client_ip}\n")
    assert caddyfile_problems(mutated) != []
