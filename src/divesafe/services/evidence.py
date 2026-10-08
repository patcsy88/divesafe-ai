"""Evidence/provenance service: gathers connector output into one validated evidence set.

It never invents or repairs evidence. A connector failure becomes an explicit issue, and the
missing category is then reported as INSUFFICIENT EVIDENCE by the rules engine.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from divesafe.data import Connector, ConnectorError, DiveSite, FetchResult
from divesafe.domain import DataCategory, EvidenceItem

logger = logging.getLogger(__name__)


class DuplicateEvidenceError(ValueError):
    """Two different evidence items claimed the same id."""


@dataclass(frozen=True)
class EvidenceSet:
    items: tuple[EvidenceItem, ...]
    issues: tuple[str, ...]
    data_versions: dict[str, str]

    def by_category(self, category: DataCategory) -> tuple[EvidenceItem, ...]:
        return tuple(i for i in self.items if i.category == category)

    @property
    def categories(self) -> frozenset[DataCategory]:
        return frozenset(i.category for i in self.items)


def build_evidence_set(
    results: Sequence[tuple[str, FetchResult | None, str | None]],
) -> EvidenceSet:
    """Merge (connector name, result, error) triples. Exactly one of result/error is set."""
    by_id: dict[str, EvidenceItem] = {}
    issues: list[str] = []
    versions: dict[str, str] = {}
    for name, result, error in results:
        if result is None:
            issues.append(f"{name}: {error or 'failed'}")
            continue
        issues.extend(f"{name}: {issue}" for issue in result.issues)
        for item in result.items:
            existing = by_id.get(item.id)
            if existing is not None and existing != item:
                raise DuplicateEvidenceError(f"conflicting evidence for id {item.id}")
            by_id[item.id] = item
            versions[item.source] = item.source_version or "unversioned"
    ordered = tuple(sorted(by_id.values(), key=lambda i: (i.category.value, i.valid_at, i.id)))
    return EvidenceSet(ordered, tuple(issues), versions)


async def gather_evidence(
    connectors: Sequence[Connector],
    site: DiveSite,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
) -> EvidenceSet:
    async def run(connector: Connector) -> tuple[str, FetchResult | None, str | None]:
        try:
            return connector.name, await connector.fetch(site, window_start, window_end, now), None
        except ConnectorError as exc:
            logger.warning("connector failed", extra={"connector": connector.name, "site": site.id})
            return connector.name, None, f"{type(exc).__name__}: {exc}"

    return build_evidence_set(await asyncio.gather(*(run(c) for c in connectors)))
