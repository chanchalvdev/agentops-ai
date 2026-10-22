import uuid
from typing import Literal

from pydantic import BaseModel, Field

RetrievalMode = Literal["vector", "keyword", "hybrid"]
MetadataValue = str | int | float | bool


class RetrievalFilters(BaseModel):
    document_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    # Exact-match containment on document metadata, e.g. {"team": "payments"}.
    metadata: dict[str, MetadataValue] | None = None


class Candidate(BaseModel):
    """One row from a single retriever, before fusion."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    document_version: int
    chunk_index: int
    text: str
    text_hash: str
    section: str | None
    page_start: int | None
    page_end: int | None
    score: float


class RetrievedChunk(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    document_version: int
    chunk_index: int
    text: str
    section: str | None
    page_start: int | None
    page_end: int | None
    score: float  # final ranking score (RRF for hybrid, raw score otherwise)
    vector_rank: int | None = None
    vector_score: float | None = None  # cosine similarity
    keyword_rank: int | None = None
    keyword_score: float | None = None  # ts_rank_cd


class RetrievalDiagnostics(BaseModel):
    mode: RetrievalMode
    vector_candidates: int = 0
    keyword_candidates: int = 0
    duplicates_removed: int = 0
    embed_ms: float = 0.0
    vector_ms: float = 0.0
    keyword_ms: float = 0.0
    total_ms: float = 0.0


class RetrievalResult(BaseModel):
    query: str
    results: list[RetrievedChunk]
    diagnostics: RetrievalDiagnostics
