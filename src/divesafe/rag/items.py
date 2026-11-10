"""Knowledge evidence items: the run form (with text) and the record form (ADR 0022 decisions 6,
15 and 16). Never a title, anywhere."""

from __future__ import annotations

from datetime import datetime

from divesafe.domain import (
    DataCategory,
    DataKind,
    DataQuality,
    DiveSite,
    EvidenceItem,
    TransformationStep,
    knowledge_ref,
)
from divesafe.rag.selection import valid_from, valid_until
from divesafe.rag.store import ChunkRow

SITE_SCOPE = "site"
ALL_SITES_SCOPE = "all_sites"

# Fixed provenance wording. The all-sites note is the item's own scope marker (decision 6): an
# all-sites document has no location, and says so, so it is never read as about this site.
QUALITY_NOTE = (
    "curated site document: context only, never live conditions; not checked against the "
    "conditions of this dive"
)
ALL_SITES_NOTE = "all-sites document: general guidance, not specific to this site"
CHUNKING = TransformationStep(
    step="chunked",
    detail="document split into sections and chunks at load; chunk text stored unchanged",
)


def evidence_id(document_id: str, version: int, chunk: int) -> str:
    return f"kb:{document_id}:{version}:{chunk}"


def source_label(document_id: str) -> str:
    """The fixed `source` label (decision 6); `data_versions` is keyed by it, never a title."""
    return f"kb:{document_id}"


def scope(item: EvidenceItem) -> str:
    """The scope marker of a knowledge item: an all-sites document has no location."""
    return ALL_SITES_SCOPE if item.location is None else SITE_SCOPE


def run_item(row: ChunkRow, site: DiveSite, now: datetime) -> EvidenceItem:
    """The run form. Raises `ValueError` (pydantic's `ValidationError` included) for a row whose
    id does not follow the decision 6 grammar or that fails the item schema."""
    item_id = evidence_id(row.document_id, row.version, row.chunk)
    if knowledge_ref(item_id) is None:  # raises for a malformed reserved id
        raise ValueError("not a knowledge id")
    notes = (QUALITY_NOTE,) if not row.all_sites else (QUALITY_NOTE, ALL_SITES_NOTE)
    return EvidenceItem(
        id=item_id,
        # Builder choice (ADR 0022, reviewed by risk-reviewer): a site document is site
        # information, an all-sites document local guidance.
        category=DataCategory.LOCAL_GUIDANCE if row.all_sites else DataCategory.SITE_INFORMATION,
        source=source_label(row.document_id),
        source_version=str(row.version),
        retrieved_at=now,
        valid_at=valid_from(row.effective_date),
        valid_until=valid_until(row.review_by_date),
        is_forecast=False,
        data_kind=DataKind.KNOWLEDGE,
        location=None if row.all_sites else site.location,
        upstream_known=False,
        quality=DataQuality.DEGRADED,
        quality_notes=notes,
        transformations=(CHUNKING,),
        value={
            "text": row.text,
            "document_id": row.document_id,
            "version": row.version,
            "section": row.section,
            "chunk": row.chunk,
            "content_sha256": row.content_sha256,
            "origin": row.origin,
            "licence": row.licence,
            "attribution": row.attribution,
            "author_role": row.author_role,
            "approver_role": row.approver_role,
        },
    )


RECORD_VALUE_KEYS = ("document_id", "version", "chunk", "content_sha256")


def record_form(item: EvidenceItem) -> EvidenceItem:
    """The record form of a knowledge item (decision 16): `value` keeps only document id,
    version, chunk and the text hash; the envelope is unchanged, so re-assessing stored evidence
    gives the same result. Any other item is returned as it is."""
    if knowledge_ref(item.id) is None:
        return item
    value = {key: item.value[key] for key in RECORD_VALUE_KEYS}
    return EvidenceItem.model_validate({**item.model_dump(), "value": value})
