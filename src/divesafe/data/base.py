"""Connector contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from divesafe.data.sites import Site
from divesafe.domain import EvidenceItem


@dataclass(frozen=True)
class FetchResult:
    """Evidence plus anything the connector could not provide.

    `issues` must be surfaced to the user. A category named in `issues` has no evidence at all,
    never partial evidence.
    """

    items: tuple[EvidenceItem, ...]
    issues: tuple[str, ...] = ()


class Connector(Protocol):
    name: str

    async def fetch(
        self, site: Site, window_start: datetime, window_end: datetime, now: datetime
    ) -> FetchResult: ...
