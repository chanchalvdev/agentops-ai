"""Document ingestion: validate -> extract -> store (request path), then chunk -> embed -> index.

Two phases on purpose:
- `create_document` runs inside the upload request. Cheap and deterministic, so bad files fail
  fast with a 4xx and the client never polls for a document that could not work.
- `process_document` runs in the background. Slow (embedding), so the API returns 202 and the
  client polls status.

Invariant: a document is READY only if every chunk has an embedding and all chunks were committed
in the same transaction that flipped the status. Any failure leaves it FAILED with zero chunks.
"""

import asyncio
import contextlib
import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import undefer

from app.core.config import Settings
from app.core.errors import (
    AppError,
    ConflictError,
    EmbeddingError,
    EmptyDocumentError,
    FileTooLargeError,
    NotFoundError,
)
from app.db.models import Document, DocumentChunk, DocumentStatus
from app.ingestion.chunking import Chunk, ChunkingConfig, chunk_text, stable_chunk_id
from app.ingestion.embeddings import Embedder
from app.ingestion.parsers import extract_text, sanitize_filename

logger = logging.getLogger(__name__)


def embedding_input(filename: str, chunk: Chunk) -> str:
    """Prefix chunk text with its document and section so isolated chunks keep their context."""
    header = f"{filename} — {chunk.section}" if chunk.section else filename
    return f"{header}\n\n{chunk.text}"


class IngestionService:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], embedder: Embedder, settings: Settings
    ) -> None:
        self._sessions, self._embedder, self._settings = sessionmaker, embedder, settings

    @property
    def chunking_config(self) -> ChunkingConfig:
        s = self._settings
        return ChunkingConfig(s.chunk_strategy, s.chunk_size_chars, s.chunk_overlap_chars)

    # --- request path --------------------------------------------------------------------------

    async def create_document(
        self,
        tenant_id: str,
        filename: str | None,
        data: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Document, bool]:
        """Returns (document, created). created=False means identical content already exists."""
        if len(data) > self._settings.max_upload_bytes:
            raise FileTooLargeError(
                f"File exceeds the {self._settings.max_upload_bytes // (1024 * 1024)} MB limit"
            )
        name = sanitize_filename(filename)
        extracted = extract_text(name, data)
        content_hash = hashlib.sha256(data).hexdigest()

        async with self._sessions() as session:
            existing = await self._by_hash(session, tenant_id, content_hash)
            if existing:
                return await self._resurface(existing), False
            latest_version = await session.scalar(
                select(func.max(Document.version)).where(
                    Document.tenant_id == tenant_id, Document.filename == name
                )
            )
            doc = Document(
                tenant_id=tenant_id,
                filename=name,
                mime_type=extracted.mime_type,
                size_bytes=len(data),
                content_hash=content_hash,
                version=(latest_version or 0) + 1,
                is_latest=False,
                status=DocumentStatus.PENDING,
                page_count=extracted.page_count,
                chunk_count=0,
                metadata_=metadata or {},
                extracted_text=extracted.text,
            )
            session.add(doc)
            try:
                await session.commit()
            except IntegrityError as exc:
                # A concurrent upload won the race. Same bytes -> return theirs; same filename
                # version collision -> let the client retry.
                await session.rollback()
                existing = await self._by_hash(session, tenant_id, content_hash)
                if existing:
                    return existing, False
                raise ConflictError(
                    "A concurrent upload of this file is in progress; retry"
                ) from exc
            logger.info(
                "document created",
                extra={"document_id": str(doc.id), "doc_version": doc.version, "bytes": len(data)},
            )
            return doc, True

    async def _resurface(self, doc: Document) -> Document:
        """Identical bytes uploaded again. An older indexed version becomes the latest again
        (the user is restoring it); a FAILED one is queued for another attempt."""
        if doc.status == DocumentStatus.FAILED:
            with contextlib.suppress(ConflictError):
                return await self.mark_for_reindex(doc.tenant_id, doc.id)
        elif doc.searchable and not doc.is_latest:
            async with self._sessions() as session, session.begin():
                await session.execute(
                    update(Document)
                    .where(Document.tenant_id == doc.tenant_id, Document.filename == doc.filename)
                    .values(is_latest=Document.id == doc.id)
                )
            logger.info("older version restored as latest", extra={"document_id": str(doc.id)})
        return await self.get(doc.tenant_id, doc.id)

    @staticmethod
    async def _by_hash(session: AsyncSession, tenant_id: str, content_hash: str) -> Document | None:
        doc: Document | None = await session.scalar(
            select(Document).where(
                Document.tenant_id == tenant_id, Document.content_hash == content_hash
            )
        )
        return doc

    # --- background path -----------------------------------------------------------------------

    async def _claim(self, document_id: uuid.UUID) -> Document | None:
        """Atomically take a queued document (or one whose worker's lease ran out). Exactly one
        caller wins, whether that is the upload's own task, the worker, or the CLI."""
        now = datetime.now(UTC)
        async with self._sessions() as session, session.begin():
            doc: Document | None = await session.scalar(
                update(Document)
                .where(
                    Document.id == document_id,
                    or_(
                        Document.status == DocumentStatus.PENDING,
                        (Document.status == DocumentStatus.PROCESSING)
                        & (Document.lease_until < now),
                    ),
                )
                .values(
                    status=DocumentStatus.PROCESSING,
                    error=None,
                    attempts=Document.attempts + 1,
                    lease_until=now + timedelta(seconds=self._settings.indexing_lease_s),
                )
                .returning(Document)
                .options(undefer(Document.extracted_text))
            )
        return doc

    async def _wait_for(self, document_id: uuid.UUID) -> DocumentStatus:
        """Another worker holds the document: wait for its outcome (bounded by the lease)."""
        deadline = asyncio.get_running_loop().time() + self._settings.indexing_lease_s
        while True:
            async with self._sessions() as session:
                status = await session.scalar(
                    select(Document.status).where(Document.id == document_id)
                )
            if status is None:
                return DocumentStatus.FAILED
            if status in (DocumentStatus.READY, DocumentStatus.FAILED):
                return status
            if asyncio.get_running_loop().time() > deadline:
                return status
            await asyncio.sleep(0.2)

    async def _heartbeat(self, document_id: uuid.UUID) -> None:
        lease = self._settings.indexing_lease_s
        while True:
            await asyncio.sleep(lease / 3)
            async with self._sessions() as session, session.begin():
                await session.execute(
                    update(Document)
                    .where(Document.id == document_id, Document.status == DocumentStatus.PROCESSING)
                    .values(lease_until=datetime.now(UTC) + timedelta(seconds=lease))
                )

    async def pending_work(self, grace_s: float, limit: int = 20) -> list[uuid.UUID]:
        """Documents a worker should pick up: queued for longer than `grace_s` (the request's
        own task should have taken them) or processing with an expired lease."""
        now = datetime.now(UTC)
        async with self._sessions() as session:
            rows = await session.scalars(
                select(Document.id)
                .where(
                    or_(
                        (Document.status == DocumentStatus.PENDING)
                        & (Document.updated_at < now - timedelta(seconds=grace_s)),
                        (Document.status == DocumentStatus.PROCESSING)
                        & (Document.lease_until < now),
                    )
                )
                .order_by(Document.updated_at)
                .limit(limit)
            )
            return list(rows)

    async def process_document(self, document_id: uuid.UUID) -> DocumentStatus:
        doc = await self._claim(document_id)
        if doc is None:
            return await self._wait_for(document_id)
        if doc.attempts > self._settings.indexing_max_attempts:
            return await self._fail(
                document_id,
                f"Gave up after {doc.attempts - 1} attempts (the process indexing it kept dying)",
                keep_index=doc.searchable,
            )
        heartbeat = asyncio.create_task(self._heartbeat(document_id))
        try:
            return await self._index(doc)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _fail(self, document_id: uuid.UUID, safe: str, *, keep_index: bool) -> DocumentStatus:
        async with self._sessions() as session, session.begin():
            if keep_index:
                # A reindex failed: the previous chunks were never touched and keep serving.
                await session.execute(
                    update(Document)
                    .where(Document.id == document_id)
                    .values(
                        status=DocumentStatus.READY,
                        error=f"Reindex failed; the previous index is still served: {safe}",
                        lease_until=None,
                        attempts=0,
                    )
                )
                return DocumentStatus.READY
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
            )
            await session.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(
                    status=DocumentStatus.FAILED,
                    error=safe,
                    chunk_count=0,
                    is_latest=False,
                    searchable=False,
                    lease_until=None,
                )
            )
        return DocumentStatus.FAILED

    async def _index(self, doc: Document) -> DocumentStatus:
        config = self.chunking_config
        document_id = doc.id
        filename, tenant_id, text = doc.filename, doc.tenant_id, doc.extracted_text
        version, was_searchable = doc.version, doc.searchable

        try:
            chunks = chunk_text(text, config)
            if not chunks:
                raise EmptyDocumentError("Document produced no chunks")
            vectors = await self._embedder.embed_documents(
                [embedding_input(filename, c) for c in chunks]
            )
            if len(vectors) != len(chunks):  # defense in depth; embedders validate too
                raise EmbeddingError("Missing embeddings for some chunks")

            async with self._sessions() as session, session.begin():
                await session.execute(
                    delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
                )
                session.add_all(
                    DocumentChunk(
                        id=stable_chunk_id(document_id, config, c),
                        document_id=document_id,
                        tenant_id=tenant_id,
                        chunk_index=c.index,
                        text=c.text,
                        text_hash=c.text_hash,
                        section=c.section,
                        page_start=c.page_start,
                        page_end=c.page_end,
                        char_start=c.char_start,
                        char_end=c.char_end,
                        embedding=v,
                        embedding_model=self._embedder.model,
                    )
                    for c, v in zip(chunks, vectors, strict=True)
                )
                # Older versions of the same file stop being searchable only now, once the new
                # version is fully indexed. Reindexing an old version must not demote a newer one.
                newer_ready = await session.scalar(
                    select(func.count())
                    .select_from(Document)
                    .where(
                        Document.tenant_id == tenant_id,
                        Document.filename == filename,
                        Document.version > version,
                        Document.searchable.is_(True),
                    )
                )
                if not newer_ready:
                    await session.execute(
                        update(Document)
                        .where(
                            Document.tenant_id == tenant_id,
                            Document.filename == filename,
                            Document.id != document_id,
                        )
                        .values(is_latest=False)
                    )
                await session.execute(
                    update(Document)
                    .where(Document.id == document_id)
                    .values(
                        status=DocumentStatus.READY,
                        error=None,
                        is_latest=not newer_ready,
                        searchable=True,
                        attempts=0,
                        lease_until=None,
                        chunk_count=len(chunks),
                        embedding_model=self._embedder.model,
                        chunking_config=config.key,
                    )
                )
        except Exception as exc:
            safe = exc.message if isinstance(exc, AppError) else "Internal ingestion error"
            logger.exception(
                "document ingestion failed",
                extra={
                    "document_id": str(document_id),
                    "error_code": getattr(exc, "code", type(exc).__name__),
                },
            )
            return await self._fail(document_id, safe, keep_index=was_searchable)

        logger.info(
            "document indexed",
            extra={"document_id": str(document_id), "chunks": len(chunks), "chunking": config.key},
        )
        return DocumentStatus.READY

    # --- reads and lifecycle (always tenant-scoped) ---------------------------------------------

    async def corpus_version(self, tenant_id: str) -> str:
        """Changes whenever the tenant's corpus changes (insert, delete, reindex, status change):
        part of every cache key that depends on document content, so a cached result can never
        outlive the documents it was computed from. Read from the database, so a change made by
        another process (the CLI) invalidates this process's cache too."""
        async with self._sessions() as session:
            count, latest = (
                await session.execute(
                    select(func.count(), func.max(Document.updated_at)).where(
                        Document.tenant_id == tenant_id
                    )
                )
            ).one()
        return f"{count}:{latest.isoformat() if latest else '-'}"

    async def get(self, tenant_id: str, document_id: uuid.UUID) -> Document:
        async with self._sessions() as session:
            doc: Document | None = await session.scalar(
                select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
            )
        if doc is None:
            # Same response for "missing" and "belongs to another tenant": no existence leak.
            raise NotFoundError("Document not found")
        return doc

    async def chunk(
        self, tenant_id: str, document_id: uuid.UUID, chunk_id: uuid.UUID
    ) -> tuple[Document, DocumentChunk]:
        """One source passage, e.g. to open a citation. Both the document and the chunk must
        belong to the tenant; anything else is the same 404 as a missing chunk."""
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(Document, DocumentChunk)
                    .join(DocumentChunk, DocumentChunk.document_id == Document.id)
                    .where(
                        Document.id == document_id,
                        Document.tenant_id == tenant_id,
                        DocumentChunk.id == chunk_id,
                        DocumentChunk.tenant_id == tenant_id,
                    )
                )
            ).one_or_none()
        if row is None:
            raise NotFoundError("Chunk not found")
        return row[0], row[1]

    async def list_documents(
        self, tenant_id: str, *, limit: int, offset: int, status: DocumentStatus | None = None
    ) -> tuple[list[Document], int]:
        conditions = [Document.tenant_id == tenant_id]
        if status:
            conditions.append(Document.status == status)
        async with self._sessions() as session:
            total = await session.scalar(
                select(func.count()).select_from(Document).where(*conditions)
            )
            rows = await session.scalars(
                select(Document)
                .where(*conditions)
                .order_by(Document.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            return list(rows), total or 0

    async def delete(self, tenant_id: str, document_id: uuid.UUID) -> None:
        """Delete one version. Deleting the latest version makes the newest remaining indexed
        version of the same file latest again, so the file does not vanish from search."""
        async with self._sessions() as session, session.begin():
            gone = (
                await session.execute(
                    delete(Document)
                    .where(Document.id == document_id, Document.tenant_id == tenant_id)
                    .returning(Document.filename, Document.is_latest)
                )
            ).one_or_none()
            if gone is None:
                raise NotFoundError("Document not found")
            filename, was_latest = gone
            if was_latest:
                previous = await session.scalar(
                    select(Document.id)
                    .where(
                        Document.tenant_id == tenant_id,
                        Document.filename == filename,
                        Document.searchable.is_(True),
                    )
                    .order_by(Document.version.desc())
                    .limit(1)
                )
                if previous is not None:
                    await session.execute(
                        update(Document).where(Document.id == previous).values(is_latest=True)
                    )

    async def mark_for_reindex(self, tenant_id: str, document_id: uuid.UUID) -> Document:
        # One conditional UPDATE: two concurrent reindex requests cannot both pass the check.
        async with self._sessions() as session, session.begin():
            claimed = await session.scalar(
                update(Document)
                .where(
                    Document.id == document_id,
                    Document.tenant_id == tenant_id,
                    Document.status.not_in([DocumentStatus.PENDING, DocumentStatus.PROCESSING]),
                )
                # searchable is left alone: the current index serves until the new one lands.
                .values(status=DocumentStatus.PENDING, error=None, attempts=0, lease_until=None)
                .returning(Document.id)
            )
        doc = await self.get(tenant_id, document_id)  # NotFoundError if missing / other tenant
        if claimed is None:
            raise ConflictError("Document is already being indexed")
        return doc
