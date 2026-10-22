"""Hybrid retrieval: vector + keyword candidates fused with Reciprocal Rank Fusion.

RRF uses ranks, not raw scores, because cosine similarity and ts_rank_cd live on incomparable
scales. score(d) = sum over retrievers of 1 / (k + rank_r(d)); k=60 damps the influence of the
very top ranks (Cormack et al., 2009).
"""

import time
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError, RetrievalError
from app.ingestion.embeddings import Embedder
from app.retrieval.keyword_search import keyword_search
from app.retrieval.schemas import (
    Candidate,
    RetrievalDiagnostics,
    RetrievalFilters,
    RetrievalMode,
    RetrievalResult,
    RetrievedChunk,
)
from app.retrieval.vector_search import vector_search


def _chunk(c: Candidate, score: float) -> RetrievedChunk:
    return RetrievedChunk(**c.model_dump(exclude={"score", "text_hash"}), score=score)


def reciprocal_rank_fusion(
    vector: Sequence[Candidate], keyword: Sequence[Candidate], k: int = 60
) -> list[RetrievedChunk]:
    fused: dict[str, RetrievedChunk] = {}
    for name, ranked in (("vector", vector), ("keyword", keyword)):
        for rank, cand in enumerate(ranked, start=1):
            key = str(cand.chunk_id)
            item = fused.setdefault(key, _chunk(cand, 0.0))
            item.score += 1.0 / (k + rank)
            setattr(item, f"{name}_rank", rank)
            setattr(item, f"{name}_score", cand.score)
    return sorted(fused.values(), key=lambda c: (-c.score, str(c.chunk_id)))


def dedupe(
    chunks: list[RetrievedChunk], hashes: dict[str, str]
) -> tuple[list[RetrievedChunk], int]:
    """Drop chunks whose text is identical to a better-ranked one (e.g. boilerplate repeated
    across documents). Keeps the first, so the list must already be ranked."""
    seen: set[str] = set()
    out = []
    for c in chunks:
        h = hashes[str(c.chunk_id)]
        if h not in seen:
            seen.add(h)
            out.append(c)
    return out, len(chunks) - len(out)


class HybridRetriever:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        *,
        candidate_k: int = 30,
        rrf_k: int = 60,
    ) -> None:
        self._sessions, self._embedder = sessionmaker, embedder
        self._candidate_k, self._rrf_k = candidate_k, rrf_k

    async def search(
        self,
        tenant_id: str,
        query: str,
        *,
        top_k: int = 6,
        mode: RetrievalMode = "hybrid",
        filters: RetrievalFilters | None = None,
    ) -> RetrievalResult:
        filters = filters or RetrievalFilters()
        diag = RetrievalDiagnostics(mode=mode)
        start = time.perf_counter()
        vec: list[Candidate] = []
        kw: list[Candidate] = []
        limit = max(top_k, self._candidate_k) if mode == "hybrid" else top_k
        try:
            if mode in ("vector", "hybrid"):
                t = time.perf_counter()
                query_vector = await self._embedder.embed_query(query)
                diag.embed_ms = (time.perf_counter() - t) * 1000
            async with self._sessions() as session, session.begin():
                if mode in ("vector", "hybrid"):
                    t = time.perf_counter()
                    vec = await vector_search(session, tenant_id, query_vector, filters, limit)
                    diag.vector_ms = (time.perf_counter() - t) * 1000
                if mode in ("keyword", "hybrid"):
                    t = time.perf_counter()
                    kw = await keyword_search(session, tenant_id, query, filters, limit)
                    diag.keyword_ms = (time.perf_counter() - t) * 1000
        except AppError:
            raise
        except Exception as exc:
            raise RetrievalError("Retrieval failed", detail=repr(exc)) from exc

        diag.vector_candidates, diag.keyword_candidates = len(vec), len(kw)
        hashes = {str(c.chunk_id): c.text_hash for c in [*vec, *kw]}
        if mode == "hybrid":
            ranked = reciprocal_rank_fusion(vec, kw, self._rrf_k)
        else:
            source = vec if mode == "vector" else kw
            ranked = [_chunk(c, c.score) for c in source]
            for rank, item in enumerate(ranked, start=1):
                setattr(item, f"{mode}_rank", rank)
                setattr(item, f"{mode}_score", item.score)
        results, diag.duplicates_removed = dedupe(ranked, hashes)
        diag.total_ms = (time.perf_counter() - start) * 1000
        return RetrievalResult(query=query, results=results[:top_k], diagnostics=diag)
