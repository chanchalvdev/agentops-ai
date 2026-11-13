"""Optional semantic answer cache (product.txt 12, item 7). Off unless explicitly enabled.

A question whose embedding is at least `min_similarity` cosine-similar to a cached question gets
the cached answer, but only inside the same scope: tenant, permissions, allowed tools, mode,
corpus version, and the full model/prompt/retrieval configuration. Any document change moves the
corpus version, so an answer can never outlive the passages it cites.

What is stored is deliberately narrow: answered + entailment-verified graph runs that used only
read-only document tools. Workspace actions, approvals, unverified loop answers, and refusals are
never reused. The threshold is conservative (0.97 by default): "RPO in v1" and "RPO in v2" embed
close together, so a loose threshold would return the wrong version's answer.
"""

import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import Any

from app.agents.result import AgentRunResult
from app.core.execution_context import ExecutionContext
from app.ingestion.embeddings import Embedder

READ_ONLY_TOOLS = frozenset({"document_search", "document_metadata", "calculator"})


@dataclass
class CachedAnswer:
    question: str
    answer: str
    citations: list[dict[str, Any]]
    tool_references: list[dict[str, Any]]
    source_execution_id: str
    similarity: float = 1.0


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def cacheable(result: AgentRunResult) -> bool:
    if result.mode != "graph" or result.status != "answered" or result.verification != "supported":
        return False
    for event in result.ledger:
        if event.kind == "approval":
            return False
        if event.kind == "tool_call" and event.name not in READ_ONLY_TOOLS:
            return False
    return True


class SemanticCache:
    def __init__(
        self,
        embedder: Embedder,
        corpus_version: Callable[[str], Awaitable[str]],
        config_fingerprint: str,
        *,
        min_similarity: float = 0.97,
        ttl_s: float = 3600.0,
        max_entries: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._embedder, self._corpus_version = embedder, corpus_version
        self._fingerprint = config_fingerprint
        self.min_similarity, self._ttl, self._max = min_similarity, ttl_s, max_entries
        self._clock = clock
        # scope -> [(expires_at, question vector, answer)]; scopes kept in LRU order
        self._entries: OrderedDict[Hashable, list[tuple[float, list[float], CachedAnswer]]] = (
            OrderedDict()
        )
        self.hits = self.misses = 0

    async def scope(self, ctx: ExecutionContext, mode: str) -> Hashable:
        return (
            ctx.tenant_id,
            tuple(sorted(ctx.permissions)),
            tuple(sorted(ctx.allowed_tools)),
            mode,
            await self._corpus_version(ctx.tenant_id),
            self._fingerprint,
        )

    async def lookup(
        self, ctx: ExecutionContext, mode: str, question: str
    ) -> tuple[CachedAnswer | None, Hashable, list[float]]:
        scope = await self.scope(ctx, mode)
        vector = await self._embedder.embed_query(question)
        now = self._clock()
        live = [e for e in self._entries.get(scope, []) if e[0] > now]
        best: CachedAnswer | None = None
        best_sim = -1.0
        for _, cached_vector, answer in live:
            sim = cosine(vector, cached_vector)
            if sim > best_sim:
                best, best_sim = answer, sim
        if scope in self._entries:
            self._entries[scope] = live
            self._entries.move_to_end(scope)
        if best is not None and best_sim >= self.min_similarity:
            self.hits += 1
            return (
                CachedAnswer(**{**best.__dict__, "similarity": round(best_sim, 4)}),
                scope,
                vector,
            )
        self.misses += 1
        return None, scope, vector

    def store(
        self, scope: Hashable, vector: list[float], question: str, result: AgentRunResult
    ) -> bool:
        if not cacheable(result):
            return False
        entry = CachedAnswer(
            question=question,
            answer=result.answer,
            citations=[c.model_dump(mode="json") for c in result.citations],
            tool_references=[t.model_dump(mode="json") for t in result.tool_references],
            source_execution_id=str(result.execution_id),
        )
        bucket = self._entries.setdefault(scope, [])
        bucket.append((self._clock() + self._ttl, vector, entry))
        self._entries.move_to_end(scope)
        while sum(len(v) for v in self._entries.values()) > self._max:
            oldest = next(iter(self._entries))
            self._entries[oldest].pop(0)
            if not self._entries[oldest]:
                del self._entries[oldest]
        return True
