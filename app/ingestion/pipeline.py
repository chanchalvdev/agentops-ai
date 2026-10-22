"""Document ingestion: validate -> extract -> store (request path), then chunk -> embed -> index.

Two phases on purpose:
- `create_document` runs inside the upload request. Cheap and deterministic, so bad files fail
  fast with a 4xx and the client never polls for a document that could not work.
- `process_document` runs in the background. Slow (embedding), so the API returns 202 and the
  client polls status.

Invariant: a document is READY only if every chunk has an embedding and all chunks were committed
in the same transaction that flipped the status. Any failure leaves it FAILED with zero chunks.
"""

import hashlib
import logging
import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
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
                return existing, False
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

    @staticmethod
    async def _by_hash(session: AsyncSession, tenant_id: str, content_hash: str) -> Document | None:
        doc: Document | None = await session.scalar(
            select(Document).where(
                Document.tenant_id == tenant_id, Document.content_hash == content_hash
            )
        )
        return doc

    # --- background path -----------------------------------------------------------------------

    async def process_document(self, document_id: uuid.UUID) -> DocumentStatus:
        config = self.chunking_config
        async with self._sessions() as session:
            doc = await session.scalar(
                select(Document)
                .options(undefer(Document.extracted_text))
                .where(Document.id == document_id)
            )
            if doc is None:
                logger.warning(
                    "process_document: document vanished", extra={"document_id": str(document_id)}
                )
                return DocumentStatus.FAILED
            doc.status, doc.error = DocumentStatus.PROCESSING, None
            await session.commit()
            filename, tenant_id, text = doc.filename, doc.tenant_id, doc.extracted_text
            version = doc.version

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
                        Document.status == DocumentStatus.READY,
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
                        is_latest=not newer_ready,
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
            async with self._sessions() as session, session.begin():
                await session.execute(
                    delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
                )
                await session.execute(
                    update(Document)
                    .where(Document.id == document_id)
                    .values(
                        status=DocumentStatus.FAILED, error=safe, chunk_count=0, is_latest=False
                    )
                )
            return DocumentStatus.FAILED

        logger.info(
            "document indexed",
            extra={"document_id": str(document_id), "chunks": len(chunks), "chunking": config.key},
        )
        return DocumentStatus.READY

    # --- reads and lifecycle (always tenant-scoped) ---------------------------------------------

    async def get(self, tenant_id: str, document_id: uuid.UUID) -> Document:
        async with self._sessions() as session:
            doc: Document | None = await session.scalar(
                select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
            )
        if doc is None:
            # Same response for "missing" and "belongs to another tenant": no existence leak.
            raise NotFoundError("Document not found")
        return doc

    async def list(
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
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                delete(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
            )
            if result.rowcount == 0:  # type: ignore[attr-defined]
                raise NotFoundError("Document not found")

    async def mark_for_reindex(self, tenant_id: str, document_id: uuid.UUID) -> Document:
        doc = await self.get(tenant_id, document_id)
        if doc.status in (DocumentStatus.PENDING, DocumentStatus.PROCESSING):
            raise ConflictError("Document is already being indexed")
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(status=DocumentStatus.PENDING, error=None)
            )
        return await self.get(tenant_id, document_id)
