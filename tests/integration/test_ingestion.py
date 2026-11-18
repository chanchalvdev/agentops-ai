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


async def test_failed_reindex_keeps_the_previous_index_serving(
    services: Services, ingest: Ingest
) -> None:
    doc = await ingest("r.md", DOC)
    before = await chunk_count(services, doc.id)
    assert before > 0
    broken = IngestionService(services.sessions, BrokenEmbedder("raise"), services.settings)
    await services.ingestion.mark_for_reindex("tenant_a", doc.id)
    # While the reindex is queued, the document is still served from its current index.
    hits = await services.retriever.search("tenant_a", "recovery point", mode="keyword")
    assert doc.id in {h.document_id for h in hits.results}
    assert await broken.process_document(doc.id) == DocumentStatus.READY
    after = await services.ingestion.get("tenant_a", doc.id)
    assert after.searchable and after.is_latest and "previous index is still served" in after.error
    assert await chunk_count(services, doc.id) == before  # untouched, never partial


async def test_deleting_the_latest_version_restores_the_previous_one(
    services: Services, ingest: Ingest
) -> None:
    v1 = await ingest("arch.md", "Retention is seven years for audit.")
    v2 = await ingest("arch.md", "Retention is five years then cold storage.")
    await services.ingestion.delete("tenant_a", v2.id)
    assert (await services.ingestion.get("tenant_a", v1.id)).is_latest
    hits = await services.retriever.search("tenant_a", "retention years", mode="keyword")
    assert {h.document_id for h in hits.results} == {v1.id}


async def test_reuploading_an_older_versions_bytes_makes_it_latest_again(
    services: Services, ingest: Ingest
) -> None:
    v1 = await ingest("arch.md", "Retention is seven years for audit.")
    v2 = await ingest("arch.md", "Retention is five years then cold storage.")
    again, created = await services.ingestion.create_document(
        "tenant_a", "arch.md", b"Retention is seven years for audit."
    )
    assert not created and again.id == v1.id and again.is_latest
    assert not (await services.ingestion.get("tenant_a", v2.id)).is_latest


async def test_reuploading_a_failed_documents_bytes_queues_it_again(services: Services) -> None:
    broken = IngestionService(services.sessions, BrokenEmbedder("raise"), services.settings)
    doc, _ = await broken.create_document("tenant_a", "x.md", DOC.encode())
    assert await broken.process_document(doc.id) == DocumentStatus.FAILED
    again, created = await services.ingestion.create_document("tenant_a", "x.md", DOC.encode())
    assert not created and again.status == DocumentStatus.PENDING
    assert await services.ingestion.process_document(doc.id) == DocumentStatus.READY


async def test_a_document_whose_worker_died_is_reclaimed(services: Services) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    doc, _ = await services.ingestion.create_document("tenant_a", "w.md", DOC.encode())
    async with services.sessions() as s, s.begin():  # a worker claimed it, then the process died
        await s.execute(
            update(Document)
            .where(Document.id == doc.id)
            .values(
                status=DocumentStatus.PROCESSING,
                attempts=1,
                lease_until=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    assert doc.id in await services.ingestion.pending_work(grace_s=0)
    assert await services.ingestion.process_document(doc.id) == DocumentStatus.READY
    assert doc.id not in await services.ingestion.pending_work(grace_s=0)


async def test_a_document_that_keeps_killing_its_worker_is_given_up(services: Services) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    doc, _ = await services.ingestion.create_document("tenant_a", "p.md", DOC.encode())
    async with services.sessions() as s, s.begin():
        await s.execute(
            update(Document)
            .where(Document.id == doc.id)
            .values(
                status=DocumentStatus.PROCESSING,
                attempts=services.settings.indexing_max_attempts,
                lease_until=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    assert await services.ingestion.process_document(doc.id) == DocumentStatus.FAILED
    assert "Gave up" in (await services.ingestion.get("tenant_a", doc.id)).error


async def test_concurrent_processing_indexes_once(services: Services) -> None:
    import asyncio

    doc, _ = await services.ingestion.create_document("tenant_a", "c.md", DOC.encode())
    statuses = await asyncio.gather(
        *(services.ingestion.process_document(doc.id) for _ in range(3))
    )
    assert statuses == [DocumentStatus.READY] * 3
    assert (await services.ingestion.get("tenant_a", doc.id)).attempts == 0


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


async def test_concurrent_reindex_requests_claim_the_document_once(
    services: Services, ingest: Callable[..., Any]
) -> None:
    import asyncio

    from app.core.errors import ConflictError

    doc = await ingest("race.md", "# Race\n\nsome text to index")
    results = await asyncio.gather(
        *(services.ingestion.mark_for_reindex("tenant_a", doc.id) for _ in range(5)),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert sum(isinstance(r, ConflictError) for r in results) == 4
