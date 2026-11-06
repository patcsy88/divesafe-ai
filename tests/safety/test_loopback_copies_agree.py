"""`domain.models._is_loopback` (the record's egress cross-check) and `models.loopback.is_loopback`
(the transport and peer check) are separate copies because `domain` may not import `models`. If
they drifted, a record could accept a host the transport treats as remote, or the other way round
(ADR 0022 decision 15, ADR 0023). Offline."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from tests.safety.kb_fakes import mutated_module

from divesafe.domain import models as domain_models
from divesafe.models import loopback

pytestmark = pytest.mark.safety

HOSTS = [
    "localhost",
    "LOCALHOST",
    "Localhost",
    "localhost.",
    "localhost.localdomain",
    "127.0.0.1",
    "127.1.2.3",
    "127.255.255.255",
    "127.0.0.01",
    "0x7f000001",
    "0x7f.0.0.1",
    "2130706433",
    "::1",
    "0:0:0:0:0:0:0:1",
    "::ffff:127.0.0.1",
    "::ffff:7f00:1",
    "::ffff:10.0.0.1",
    "::1%lo0",
    "fe80::1%lo0",
    "[::1]",
    "0.0.0.0",
    "::",
    "10.0.0.1",
    "192.168.1.1",
    "169.254.1.1",
    "8.8.8.8",
    "2001:db8::1",
    "example.com",
    "localhost.example.com",
    "api.openai.com",
    "",
    " 127.0.0.1",
    "127.0.0.1 ",
]


def disagreements(
    domain_check: Callable[[str], bool], transport_check: Callable[[str], bool]
) -> list[str]:
    return [h for h in HOSTS if domain_check(h) != transport_check(h)]


def test_the_two_loopback_checks_agree_on_every_host() -> None:
    assert disagreements(domain_models._is_loopback, loopback.is_loopback) == []  # noqa: SLF001


def test_the_shared_list_holds_both_answers() -> None:
    answers = {loopback.is_loopback(h) for h in HOSTS}
    assert answers == {True, False}


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "        return address.ipv4_mapped.is_loopback\n",
            "        return False\n",
        ),
        (
            "    if host in _LOOPBACK_NAMES:\n        return True\n    try:\n"
            "        address = ipaddress.ip_address(host)",
            "    if host.lower() in _LOOPBACK_NAMES:\n        return True\n    try:\n"
            "        address = ipaddress.ip_address(host)",
        ),
    ],
    ids=["mapped-ipv4-not-loopback", "names-case-folded"],
)
def test_mutation_a_drifted_domain_copy_is_caught(old: str, new: str) -> None:
    mutant = mutated_module(domain_models, (old, new))
    assert disagreements(mutant._is_loopback, loopback.is_loopback) != []  # noqa: SLF001
