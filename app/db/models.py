"""ORM models. Day 1: documents and chunks; execution/eval tables arrive with their features."""

import enum
import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, deferred, mapped_column, relationship

EMBEDDING_DIM = 768  # must match settings.embedding_dim and migration 0001


class Base(DeclarativeBase):
    pass


class DocumentStatus(enum.StrEnum):
    PENDING = "pending"  # stored, waiting for chunking + embedding
    PROCESSING = "processing"
    READY = "ready"  # every chunk embedded and committed
    FAILED = "failed"  # never silently partial: failed docs have zero searchable chunks


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        # Idempotency: identical bytes uploaded twice by the same tenant -> same document.
        UniqueConstraint("tenant_id", "content_hash", name="uq_documents_tenant_hash"),
        UniqueConstraint("tenant_id", "filename", "version", name="uq_documents_tenant_file_ver"),
        Index("ix_documents_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_latest: Mapped[bool] = mapped_column(default=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus, name="document_status", values_callable=lambda e: [m.value for m in e]
        ),
        default=DocumentStatus.PENDING,
    )
    error: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    chunking_config: Mapped[str | None] = mapped_column(String(100))
    # User-supplied labels (team, doc type...). Genuinely variable shape -> JSONB.
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    # Normalized text; pages separated by \f. Kept so reindex never needs the original upload.
    extracted_text: Mapped[str] = deferred(mapped_column(Text))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunks_doc_index"),
        Index("ix_chunks_tenant_doc", "tenant_id", "document_id"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)  # deterministic, see chunking.py
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    tenant_id: Mapped[str] = mapped_column(String(64))  # denormalized for isolation filters
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(64))
    section: Mapped[str | None] = mapped_column(String(500))
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str] = mapped_column(String(100))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(section, '') || ' ' || text)", persisted=True),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="chunks")
