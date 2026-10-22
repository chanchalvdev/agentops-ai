"""Shared tenant-scoped base query and pgvector cosine search."""

from typing import Any

from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentChunk, DocumentStatus
from app.retrieval.schemas import Candidate, RetrievalFilters

type AnySelect = Select[*tuple[Any, ...]]


def scoped_select(tenant_id: str, filters: RetrievalFilters, *score: Any) -> AnySelect:
    """Every retriever starts here, so tenant isolation cannot be forgotten by one of them.
    Both the chunk and the document must belong to the tenant (defense in depth)."""
    stmt: AnySelect = (
        select(
            DocumentChunk.id,
            DocumentChunk.document_id,
            Document.filename,
            Document.version.label("document_version"),
            DocumentChunk.chunk_index,
            DocumentChunk.text,
            DocumentChunk.text_hash,
            DocumentChunk.section,
            DocumentChunk.page_start,
            DocumentChunk.page_end,
            *score,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.tenant_id == tenant_id,
            Document.tenant_id == tenant_id,
            Document.status == DocumentStatus.READY,
            Document.is_latest.is_(True),
        )
    )
    if filters.document_ids:
        stmt = stmt.where(DocumentChunk.document_id.in_(filters.document_ids))
    if filters.metadata:
        stmt = stmt.where(Document.metadata_.contains(filters.metadata))
    return stmt


def to_candidates(rows: Any) -> list[Candidate]:
    return [
        Candidate(
            chunk_id=r.id,
            document_id=r.document_id,
            filename=r.filename,
            document_version=r.document_version,
            chunk_index=r.chunk_index,
            text=r.text,
            text_hash=r.text_hash,
            section=r.section,
            page_start=r.page_start,
            page_end=r.page_end,
            score=float(r.score),
        )
        for r in rows
    ]


async def vector_search(
    session: AsyncSession,
    tenant_id: str,
    query_vector: list[float],
    filters: RetrievalFilters,
    limit: int,
) -> list[Candidate]:
    distance = DocumentChunk.embedding.cosine_distance(query_vector)
    # HNSW applies WHERE filters after the index scan. With a selective tenant filter the scan
    # can run out of candidates; iterative scan (pgvector >= 0.8) keeps scanning until `limit`
    # filtered rows are found. SET LOCAL scopes both settings to this transaction.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {max(40, min(limit * 2, 1000))}"))
    stmt: AnySelect = (
        scoped_select(tenant_id, filters, (1 - distance).label("score"))
        .order_by(distance)
        .limit(limit)
    )
    return to_candidates((await session.execute(stmt)).all())
