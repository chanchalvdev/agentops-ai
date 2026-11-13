"""ORM models. Day 1: documents and chunks. Day 2: agent executions, their events, approvals.
Day 3: workspace audit log, evaluation datasets and runs."""

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
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


class AgentExecution(Base):
    """One agent run. Counters and status are typed columns; only variable shapes are JSONB."""

    __tablename__ = "agent_executions"
    __table_args__ = (Index("ix_executions_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))  # loop | graph
    status: Mapped[str] = mapped_column(String(32))  # running | RunStatus
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    error_code: Mapped[str | None] = mapped_column(String(64))
    steps: Mapped[int] = mapped_column(Integer, default=0)
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    total_ms: Mapped[float | None] = mapped_column()
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExecutionEvent(Base):
    """Ledger entries: decisions, tool names, safe arguments, outcomes. No prompts or secrets."""

    __tablename__ = "execution_events"
    __table_args__ = (UniqueConstraint("execution_id", "seq", name="uq_events_execution_seq"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_executions.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[float | None] = mapped_column()
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Approval(Base):
    """A paused tool call waiting for a human decision (LangGraph interrupt)."""

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("execution_id", "call_id", name="uq_approvals_execution_call"),
        Index("ix_approvals_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_executions.id", ondelete="CASCADE")
    )
    tenant_id: Mapped[str] = mapped_column(String(64))
    call_id: Mapped[str] = mapped_column(String(100))
    tool_name: Mapped[str] = mapped_column(String(100))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|rejected
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkspaceAuditEvent(Base):
    """One workspace operation by the agent or a user, including rejected ones. No FK to
    agent_executions on purpose: the audit trail outlives deleted executions."""

    __tablename__ = "workspace_audit"
    __table_args__ = (Index("ix_workspace_audit_tenant_id", "tenant_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(16))  # agent | user
    execution_id: Mapped[uuid.UUID | None] = mapped_column()
    key_id: Mapped[str | None] = mapped_column(String(32))
    operation: Mapped[str] = mapped_column(String(16))
    target: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))  # ok | rejected | error
    error_code: Mapped[str | None] = mapped_column(String(64))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[float] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvaluationDataset(Base):
    """A versioned dataset file, identified by content hash: editing cases without bumping the
    version still produces a distinct row, so runs are never compared on silently changed data."""

    __tablename__ = "evaluation_datasets"
    __table_args__ = (UniqueConstraint("name", "sha256", name="uq_eval_datasets_name_sha"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(50))
    sha256: Mapped[str] = mapped_column(String(64))
    case_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvaluationCase(Base):
    __tablename__ = "evaluation_cases"
    __table_args__ = (UniqueConstraint("dataset_id", "case_id", name="uq_eval_cases_dataset_case"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_datasets.id", ondelete="CASCADE")
    )
    case_id: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)  # the full case: variable shape


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    __table_args__ = (Index("ix_eval_runs_requested_started", "requested_by", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluation_datasets.id"))
    requested_by: Mapped[str] = mapped_column(String(64))  # tenant that started it
    status: Mapped[str] = mapped_column(String(16))  # running | completed | failed
    mode: Mapped[str] = mapped_column(String(16))
    # models, prompt/workflow versions, retrieval config: what the scores are scores OF
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    aggregate: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    total_cases: Mapped[int] = mapped_column(Integer, default=0)
    passed_cases: Mapped[int] = mapped_column(Integer, default=0)
    pass_rate: Mapped[float | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"
    __table_args__ = (UniqueConstraint("run_id", "case_id", name="uq_eval_results_run_case"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluation_runs.id", ondelete="CASCADE"))
    case_id: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32))
    passed: Mapped[bool | None] = mapped_column()  # None = skipped (mode not supported)
    status: Mapped[str] = mapped_column(String(32))
    execution_id: Mapped[uuid.UUID | None] = mapped_column()
    latency_ms: Mapped[float | None] = mapped_column()
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    evaluators: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    judge: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    failures: Mapped[list[str]] = mapped_column(JSONB, default=list)
    answer: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class RetrievalEvalRun(Base):
    """Retrieval-only evaluation of the golden set: recall@k, hit rate, MRR, source mix."""

    __tablename__ = "retrieval_eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    requested_by: Mapped[str] = mapped_column(String(64))
    dataset_name: Mapped[str] = mapped_column(String(100))
    dataset_version: Mapped[str] = mapped_column(String(50))
    dataset_sha256: Mapped[str] = mapped_column(String(64))
    profile: Mapped[str | None] = mapped_column(String(64))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    cases: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DriftCheck(Base):
    __tablename__ = "drift_checks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    requested_by: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # ok | drift | suspected
    baseline: Mapped[dict[str, Any]] = mapped_column(JSONB)
    signals: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MonitoringAlert(Base):
    __tablename__ = "monitoring_alerts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    check_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("drift_checks.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(16))  # quality | retrieval | data | operational
    metric: Mapped[str] = mapped_column(String(100))
    severity: Mapped[str] = mapped_column(String(16))
    evidence: Mapped[str] = mapped_column(String(16))  # measured | suspected
    baseline_value: Mapped[float | None] = mapped_column()
    current_value: Mapped[float | None] = mapped_column()
    threshold: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
