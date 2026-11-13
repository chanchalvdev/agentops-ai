"""document_search: the agent's only way to read document content, via hybrid retrieval.

Returns raw passages with their chunk IDs. The agent, not the tool, assigns citation labels and
fences the text as untrusted evidence, so labels stay unique across several searches.
"""

import json
import uuid
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from app.core.execution_context import ExecutionContext
from app.retrieval.hybrid_search import HybridRetriever
from app.retrieval.schemas import MetadataValue, RetrievalFilters
from app.tools.base import Tool


class DocumentSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500, description="What to search for.")
    document_ids: list[uuid.UUID] | None = Field(
        default=None, max_length=20, description="Optional: only search these documents."
    )
    metadata: dict[str, MetadataValue] | None = Field(
        default=None, description='Optional exact-match labels, e.g. {"team": "payments"}.'
    )
    # Floor of 3: in a live run qwen3:8b chose top_k=1 and missed the passage it needed.
    top_k: int = Field(default=5, ge=3, le=10)


class Passage(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    document_version: int
    section: str | None
    page_start: int | None
    page_end: int | None
    score: float
    text: str


class DocumentSearchOutput(BaseModel):
    query: str
    passages: list[Passage]
    # Retrieval timings (embedding, vector, keyword) for traces; never shown to the model.
    timings_ms: dict[str, float] = Field(default_factory=dict)
    embed_cached: bool = False


class DocumentSearch(Tool[DocumentSearchInput, DocumentSearchOutput]):
    name = "document_search"
    description = (
        "Search the user's documents and return relevant passages with their sources. Call this "
        "before answering any question about document content."
    )
    input_model = DocumentSearchInput
    output_model = DocumentSearchOutput
    permissions: ClassVar[frozenset[str]] = frozenset({"documents:read"})
    timeout_s = 30.0  # includes the query embedding call

    def ledger_output(self, output: DocumentSearchOutput) -> dict[str, Any]:
        # What was retrieved, by reference: traces and evaluation need it, passage text stays out.
        return {
            "timings_ms": output.timings_ms,
            "embed_cached": output.embed_cached,
            "chunk_ids": [str(p.chunk_id) for p in output.passages],
            "document_ids": list(dict.fromkeys(str(p.document_id) for p in output.passages)),
            "filenames": list(dict.fromkeys(p.filename for p in output.passages)),
        }

    def __init__(
        self,
        retriever: HybridRetriever,
        max_passage_chars: int = 2000,
        corpus_version: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._retriever, self._max_chars = retriever, max_passage_chars
        self._corpus_version = corpus_version

    async def cache_key(self, args: DocumentSearchInput, ctx: ExecutionContext) -> Hashable | None:
        if self._corpus_version is None:
            return None  # cannot tell when the corpus changed: never cache
        return (
            await self._corpus_version(ctx.tenant_id),
            json.dumps(self._retriever.config(), sort_keys=True),
            self._max_chars,
        )

    async def run(self, args: DocumentSearchInput, ctx: ExecutionContext) -> DocumentSearchOutput:
        result = await self._retriever.search(
            ctx.tenant_id,  # tenant comes from the execution context, never from the model
            args.query,
            top_k=args.top_k,
            filters=RetrievalFilters(document_ids=args.document_ids, metadata=args.metadata),
        )
        d = result.diagnostics
        return DocumentSearchOutput(
            query=args.query,
            embed_cached=d.embed_cached,
            timings_ms={
                "embed": d.embed_ms,
                "vector": d.vector_ms,
                "keyword": d.keyword_ms,
                "total": d.total_ms,
            },
            passages=[
                Passage(
                    **c.model_dump(
                        include={
                            "chunk_id",
                            "document_id",
                            "filename",
                            "document_version",
                            "section",
                            "page_start",
                            "page_end",
                            "score",
                        }
                    ),
                    text=c.text[: self._max_chars],
                )
                for c in result.results
            ],
        )
