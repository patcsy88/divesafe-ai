"""The knowledge connector (ADR 0022 decisions 2-4, 6, 12, 14).

Selection runs in SQL (`rag.store`) and again here from the per-version summary
(`rag.selection`). A chunk is kept only when both agree that its version is the returned
governing version of a family that applies to the site; any disagreement drops the chunks and
raises a fixed issue (fails closed). Length and count are re-checked; malformed ids and items past
the bound are dropped, each with a fixed issue. Failures become issues through `ConnectorError`,
never an abort. Nothing from the corpus is logged.

ADR 0028 decision 7: every call carries the run's recorded egress basis; under
`remote_knowledge_opt_in` a family whose governing version lacks the model-host attestation
returns nothing, in SQL and in code alike, with a fixed issue.

Typed notes (decision 14) come back from `retrieve` beside the `FetchResult`; they never enter a
prompt and never force an outcome. Recording them is the Retrieve stage's job.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby

from pydantic import ValidationError

from divesafe.data import FetchResult
from divesafe.domain import (
    MAX_KNOWLEDGE_ITEMS,
    DiveSite,
    EvidenceItem,
    KnowledgeNote,
    KnowledgeNoteCode,
)
from divesafe.rag.items import run_item, screened
from divesafe.rag.selection import (
    REMOTE_KNOWLEDGE_OPT_IN,
    FamilyClass,
    VersionRow,
    classify,
    family_issues,
    family_notes,
    usable_for,
)
from divesafe.rag.store import (
    MAX_CHUNK_CHARACTERS,
    STORE_MALFORMED,
    ChunkRow,
    KnowledgeStore,
    KnowledgeStoreError,
    require_aware,
)

logger = logging.getLogger(__name__)

NAME = "knowledge"

# Fixed issue wording (decisions 12, 14 and the filter-stage conditions of decision 15).
DISAGREEMENT_ISSUE = (
    "knowledge store: the database and the code disagree on which document chunks apply; "
    "none of them used"
)
MALFORMED_ID_ISSUE = "knowledge store: a chunk with a malformed id was dropped"
TOO_LONG_ISSUE = "knowledge store: a chunk over the length bound was dropped"
SCHEMA_ISSUE = "knowledge store: a chunk that failed the evidence schema was dropped"
SCREEN_ISSUE = "knowledge store: a chunk that failed the character screen was dropped"


def bound_issue(returned: int, available: int) -> str:
    """Reaching the per-run bound is an issue (decision 12; owner decision 2026-11-09: at 20 or
    more available chunks), with the count returned and the count available."""
    return (
        f"knowledge store: the per-run bound of {MAX_KNOWLEDGE_ITEMS} chunks was reached "
        f"({returned} returned of {available} available)"
    )


@dataclass(frozen=True)
class KnowledgeResult:
    """The connector's evidence and issues (as a `FetchResult`) plus the typed notes."""

    fetch: FetchResult
    notes: tuple[KnowledgeNote, ...]


def _order(row: ChunkRow) -> tuple[str, int, int]:
    return (row.document_id, row.version, row.chunk)


def check(
    site: DiveSite,
    versions: Sequence[VersionRow],
    chunks: Sequence[ChunkRow],
    available: int,
    now: datetime,
    window_end: datetime,
    *,
    egress_basis: object,
) -> KnowledgeResult:
    """The code layer: classify, cross-check SQL, re-check every bound, build the items.
    Versions for another site are ignored (the site filter, again in code)."""
    issues: list[str] = []
    results = classify(
        (v for v in versions if v.applies_to(site.id)),
        now,
        window_end,
        egress_basis=egress_basis,
    )
    issues.extend(family_issues(results))
    notes = list(family_notes(results))

    returned = {
        (r.document_id, r.version): r.chunks for r in results if r.kind is FamilyClass.RETURNED
    }
    # The schema refuses an approval of a version with no chunks; a returned one is malformed.
    if any(n == 0 for n in returned.values()):
        raise KnowledgeStoreError(STORE_MALFORMED)
    expected = sum(returned.values())
    agreed: list[ChunkRow] = []
    disagree = expected != available
    for row in sorted(chunks, key=_order):
        if (
            (row.document_id, row.version) not in returned
            or not (row.all_sites or row.site_id == site.id)
            or not usable_for(row.review_by_date, window_end)
        ):
            disagree = True
            continue
        agreed.append(row)
    if len({_order(r) for r in agreed}) != len(agreed):
        disagree = True
    # Every chunk of every returned family, or exactly the first `MAX_KNOWLEDGE_ITEMS` of them.
    if len(agreed) not in (expected, min(expected, MAX_KNOWLEDGE_ITEMS)):
        disagree = True
    if disagree:
        issues.append(DISAGREEMENT_ISSUE)
        agreed = []

    # A family is used whole or not at all (AGENTS.md section 10: never partial data as
    # complete): one dropped chunk drops every chunk of its family, with the fixed issue.
    items: list[EvidenceItem] = []
    dropped: list[str] = []
    for _, family in groupby(agreed, key=lambda r: r.document_id):
        built: list[EvidenceItem] = []
        failed: list[str] = []
        for row in family:
            if len(row.text) > MAX_CHUNK_CHARACTERS:
                failed.append(TOO_LONG_ISSUE)
                continue
            if not screened(row):
                failed.append(SCREEN_ISSUE)
                continue
            try:
                built.append(run_item(row, site, now))
            except ValidationError:
                failed.append(SCHEMA_ISSUE)
            except ValueError:
                failed.append(MALFORMED_ID_ISSUE)
        if failed:
            dropped.extend(failed)
        else:
            items.extend(built)
    issues.extend(dict.fromkeys(dropped))

    total = max(expected, available, len(chunks))
    if total >= MAX_KNOWLEDGE_ITEMS or len(items) > MAX_KNOWLEDGE_ITEMS:
        items = items[:MAX_KNOWLEDGE_ITEMS]
        issues.append(bound_issue(len(items), total))

    if not items and not issues and not notes:
        notes.append(KnowledgeNote(code=KnowledgeNoteCode.NONE_RETURNED))
    return KnowledgeResult(FetchResult(tuple(items), tuple(issues)), tuple(notes))


class KnowledgeConnector:
    """Curated site knowledge, context only. Its items are KNOWLEDGE in `site_information` or
    `local_guidance`; they never supply live conditions."""

    name = NAME

    def __init__(self, store: KnowledgeStore) -> None:
        self._store = store

    async def retrieve(
        self,
        site: DiveSite,
        window_start: datetime,
        window_end: datetime,
        now: datetime,
        *,
        egress_basis: object,
    ) -> KnowledgeResult:
        """Raises `ConnectorError` (fixed text) when the store cannot answer. Selection reads
        only the window END (decision 3: in force by it, usable to it). `egress_basis` is the
        run's recorded basis, passed on every call (ADR 0028 decision 7), never a setting."""
        require_aware(now, window_end)
        selection = await asyncio.to_thread(
            functools.partial(
                self._store.select, site.id, now, window_end, egress_basis=egress_basis
            )
        )
        result = check(
            site,
            selection.versions,
            selection.chunks,
            selection.available,
            now,
            window_end,
            egress_basis=egress_basis,
        )
        logger.info(
            "knowledge retrieved",
            extra={
                "site": site.id,
                "items": len(result.fetch.items),
                "issues": len(result.fetch.issues),
            },
        )
        return result

    async def fetch(
        self, site: DiveSite, window_start: datetime, window_end: datetime, now: datetime
    ) -> FetchResult:
        """The `Connector` contract, which carries no basis. It always excludes unattested
        versions (ADR 0028 review nit: the strictest choice). The pipeline never uses its items:
        the knowledge connector is screened out of Observe and Retrieve calls `retrieve`."""
        result = await self.retrieve(
            site, window_start, window_end, now, egress_basis=REMOTE_KNOWLEDGE_OPT_IN
        )
        return result.fetch
