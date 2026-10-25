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

from divesafe.config.settings import CONNECTOR_BUDGET_SECONDS
from divesafe.data import Connector, ConnectorError, DiveSite, FetchResult
from divesafe.domain import DataCategory, EvidenceItem

logger = logging.getLogger(__name__)


class DuplicateEvidenceError(ValueError):
    """Two different evidence items claimed the same id. Kept exported for compatibility;
    `build_evidence_set` no longer raises it (a conflict drops the items and adds an issue, so the
    run gets more cautious instead of failing). Nothing in the package raises it now."""


@dataclass(frozen=True)
class EvidenceSet:
    items: tuple[EvidenceItem, ...]
    issues: tuple[str, ...]
    data_versions: dict[str, str]
    # Items dropped because two different items shared an id. For the audit record only: they are
    # never evaluated, cited, or passed to the engine, the agents or a prompt.
    withheld: tuple[EvidenceItem, ...] = ()

    def by_category(self, category: DataCategory) -> tuple[EvidenceItem, ...]:
        return tuple(i for i in self.items if i.category == category)

    @property
    def categories(self) -> frozenset[DataCategory]:
        return frozenset(i.category for i in self.items)


def build_evidence_set(
    results: Sequence[tuple[str, FetchResult | None, str | None]],
) -> EvidenceSet:
    """Merge (connector name, result, error) triples. Exactly one of result/error is set."""
    issues: list[str] = []
    # id -> (connector, item) in arrival order, so a third or later duplicate is seen too.
    claims: dict[str, list[tuple[str, EvidenceItem]]] = {}
    for name, result, error in results:
        if result is None:
            issues.append(f"{name}: {error or 'failed'}")
            continue
        issues.extend(f"{name}: {issue}" for issue in result.issues)
        for item in result.items:
            claims.setdefault(item.id, []).append((name, item))
    kept: list[EvidenceItem] = []
    withheld: list[EvidenceItem] = []
    versions: dict[str, str] = {}
    for claimed in claims.values():
        first = claimed[0][1]
        if any(item != first for _, item in claimed[1:]):
            # Different items under one id: use none of them, whichever connector sent them. They
            # are kept apart in `withheld` so a reviewer can see them; they may hold a more severe
            # result than anything evaluated. The issue names the connectors and the categories
            # (fixed names and enum values) and not the id or any content.
            connectors = sorted({name for name, _ in claimed})
            categories = sorted({item.category.value for _, item in claimed})
            issues.append(
                f"evidence.conflict: {', '.join(connectors)} returned different "
                f"{', '.join(categories)} evidence under one id; none of it used "
                "(withheld, not evaluated; it could hold a more severe result)"
            )
            for _, item in claimed:
                if item not in withheld:
                    withheld.append(item)
            logger.warning("conflicting evidence dropped", extra={"connectors": connectors})
            continue
        kept.append(first)
        versions[first.source] = first.source_version or "unversioned"
    ordered = tuple(sorted(kept, key=lambda i: (i.category.value, i.valid_at, i.id)))
    held = tuple(sorted(withheld, key=lambda i: (i.category.value, i.valid_at, i.id)))
    return EvidenceSet(ordered, tuple(issues), versions, held)


async def gather_evidence(
    connectors: Sequence[Connector],
    site: DiveSite,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
    *,
    timeout_seconds: float = CONNECTOR_BUDGET_SECONDS,
) -> EvidenceSet:
    """Fetch from every connector at once. A connector that has not answered within the overall
    timeout becomes an issue, like any other failure, so one stuck source cannot hold a run."""
    if not (0 < timeout_seconds <= CONNECTOR_BUDGET_SECONDS):  # also false for NaN
        raise ValueError("timeout_seconds must be positive and within the connector budget")

    async def run(connector: Connector) -> tuple[str, FetchResult | None, str | None]:
        try:
            result = await asyncio.wait_for(
                connector.fetch(site, window_start, window_end, now), timeout_seconds
            )
            return connector.name, result, None
        except TimeoutError:
            logger.warning("connector timed out", extra={"connector": connector.name})
            return connector.name, None, "TimeoutError: no answer within the time allowed"
        except ConnectorError as exc:
            logger.warning("connector failed", extra={"connector": connector.name, "site": site.id})
            return connector.name, None, f"{type(exc).__name__}: {exc}"

    return build_evidence_set(await asyncio.gather(*(run(c) for c in connectors)))
