from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import func, select

from app.core.errors import EmbeddingError, EmptyDocumentError, FileTooLargeError, NotFoundError
from app.db.models import Document, DocumentChunk, DocumentStatus
from app.ingestion.embeddings import FakeEmbedder
from app.ingestion.pipeline import IngestionService
from app.services import Services
from tests.conftest import make_pdf

pytestmark = pytest.mark.integration
Ingest = Callable[..., Any]

DOC = (
    "# Payments\n\n## Recovery\n\nThe RPO is 5 minutes and the RTO is 15 minutes.\n\n"
    + "## Retention\n\n"
    + "Records are retained for five years in the primary store. " * 20
)


async def chunk_count(services: Services, document_id: object) -> int:
    async with services.sessions() as s:
        return (
            await s.scalar(
                select(func.count())
                .select_from(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
            )
            or 0
        )


async def test_ingest_markdown_end_to_end(services: Services, ingest: Ingest) -> None:
    doc = await ingest("payments.md", DOC, metadata={"team": "payments"})
    assert doc.status == DocumentStatus.READY and doc.is_latest and doc.version == 1
    assert doc.chunk_count > 1 and doc.chunk_count == await chunk_count(services, doc.id)
    assert doc.embedding_model == "fake-embed" and doc.chunking_config == "structured:400:80"
    async with services.sessions() as s:
        chunks = (
            await s.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == doc.id)
                .order_by(DocumentChunk.chunk_index)
            )
        ).all()
    assert all(len(c.embedding) == 768 and c.tenant_id == "tenant_a" for c in chunks)
    assert any("Recovery" in (c.section or "") for c in chunks)


async def test_pdf_pages_are_preserved(services: Services, ingest: Ingest) -> None:
    doc = await ingest("spec.pdf", make_pdf(["Alpha requirements page", "Beta failover page"]))
    assert doc.status == DocumentStatus.READY and doc.page_count == 2
    top = (await services.retriever.search("tenant_a", "failover", mode="keyword")).results[0]
    assert (top.page_start, top.page_end) == (1, 2)  # both short pages fit in one chunk


async def test_duplicate_upload_is_idempotent(services: Services, ingest: Ingest) -> None:
    first = await ingest("a.md", DOC)
    doc, created = await services.ingestion.create_document("tenant_a", "renamed.md", DOC.encode())
    assert not created and doc.id == first.id
    # Same bytes for a different tenant is a different document.
    _, created_b = await services.ingestion.create_document("tenant_b", "a.md", DOC.encode())
    assert created_b


async def test_new_content_same_filename_creates_new_version(
    services: Services, ingest: Ingest
) -> None:
    v1 = await ingest("arch.md", "Retention is seven years for audit.")
    v2 = await ingest("arch.md", "Retention is five years then cold storage.")
    assert (v1.version, v2.version) == (1, 2)
    v1 = await services.ingestion.get("tenant_a", v1.id)
    assert not v1.is_latest and v2.is_latest
    hits = await services.retriever.search("tenant_a", "retention years", mode="keyword")
    assert {h.document_id for h in hits.results} == {v2.id}  # superseded version not searchable

    # Reindexing the old version must not make it "latest" again.
    await services.ingestion.mark_for_reindex("tenant_a", v1.id)
    await services.ingestion.process_document(v1.id)
    assert not (await services.ingestion.get("tenant_a", v1.id)).is_latest
    assert (await services.ingestion.get("tenant_a", v2.id)).is_latest


class BrokenEmbedder(FakeEmbedder):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.mode == "raise":
            raise EmbeddingError("model crashed")
        return (await super().embed_documents(texts))[:-1]  # silently drops one vector


@pytest.mark.parametrize("mode", ["raise", "missing"])
async def test_embedding_failure_never_marks_ready(services: Services, mode: str) -> None:
    broken = IngestionService(services.sessions, BrokenEmbedder(mode), services.settings)
    doc, _ = await broken.create_document("tenant_a", "x.md", DOC.encode())
    assert await broken.process_document(doc.id) == DocumentStatus.FAILED
    doc = await services.ingestion.get("tenant_a", doc.id)
    assert doc.status == DocumentStatus.FAILED and doc.error and doc.chunk_count == 0
    assert await chunk_count(services, doc.id) == 0


async def test_failed_reindex_leaves_no_partial_chunks(services: Services, ingest: Ingest) -> None:
    doc = await ingest("r.md", DOC)
    assert await chunk_count(services, doc.id) > 0
    broken = IngestionService(services.sessions, BrokenEmbedder("raise"), services.settings)
    await services.ingestion.mark_for_reindex("tenant_a", doc.id)
    await broken.process_document(doc.id)
    assert (await services.ingestion.get("tenant_a", doc.id)).status == DocumentStatus.FAILED
    assert await chunk_count(services, doc.id) == 0


async def test_empty_and_oversized_uploads_rejected_before_storage(services: Services) -> None:
    with pytest.raises(EmptyDocumentError):
        await services.ingestion.create_document("tenant_a", "e.md", b"  \n ")
    with pytest.raises(FileTooLargeError):
        await services.ingestion.create_document(
            "tenant_a", "big.txt", b"x" * (services.settings.max_upload_bytes + 1)
        )
    async with services.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Document)) == 0


async def test_delete_cascades_and_is_tenant_scoped(services: Services, ingest: Ingest) -> None:
    doc = await ingest("d.md", DOC)
    with pytest.raises(NotFoundError):
        await services.ingestion.delete("tenant_b", doc.id)
    await services.ingestion.delete("tenant_a", doc.id)
    assert await chunk_count(services, doc.id) == 0
    with pytest.raises(NotFoundError):
        await services.ingestion.get("tenant_a", doc.id)
