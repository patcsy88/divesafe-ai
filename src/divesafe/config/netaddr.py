"""Client address normalisation shared by settings and the failed-authentication limiter."""

from __future__ import annotations

import ipaddress

_MAX_TEXT = 64


def canonical_ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an address, dropping any IPv6 zone id and unwrapping IPv4-mapped IPv6, so one
    machine has one spelling. Anything unparseable or oversized is None, never a guess."""
    cleaned = text.strip().split("%", 1)[0]
    if not cleaned or len(cleaned) > _MAX_TEXT:
        return None
    try:
        address = ipaddress.ip_address(cleaned)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def bucket(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """The unit a limit is counted against: one IPv4 address, or one IPv6 /64, because a single
    IPv6 subscriber controls 2**64 addresses and a per-address limit would be no limit."""
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{address}/64", strict=False).network_address)
    return str(address)
