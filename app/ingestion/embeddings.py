"""Embedding providers.

Documents and queries are embedded differently on purpose: nomic-embed-text is trained with task
prefixes ("search_document: " / "search_query: ") and retrieval quality drops without them.
"""

import hashlib
import math
import re
from typing import Protocol

import httpx

from app.core.errors import AppError, EmbeddingError
from app.core.retry import retry_async
from app.llm.provider import _map_http_error

_TOKEN = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    model: str
    dim: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


def _validate(vectors: list[list[float]], expected: int, dim: int) -> list[list[float]]:
    if len(vectors) != expected:
        raise EmbeddingError(f"Expected {expected} embeddings, got {len(vectors)}")
    for vec in vectors:
        if len(vec) != dim:
            raise EmbeddingError(f"Embedding has dimension {len(vec)}, expected {dim}")
        if not all(math.isfinite(x) for x in vec):
            raise EmbeddingError("Embedding contains NaN or infinity")
    return vectors


class OllamaEmbedder:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
        dim: int,
        batch_size: int = 32,
        timeout_s: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        self._client, self._base = client, base_url.rstrip("/")
        self.model, self.dim = model, dim
        self._batch, self._timeout, self._retries = batch_size, timeout_s, max_retries
        nomic = "nomic" in model
        self._doc_prefix = "search_document: " if nomic else ""
        self._query_prefix = "search_query: " if nomic else ""

    async def _embed_batch(self, inputs: list[str]) -> list[list[float]]:
        async def call() -> list[list[float]]:
            try:
                resp = await self._client.post(
                    f"{self._base}/api/embed",
                    timeout=self._timeout,
                    json={"model": self.model, "input": inputs, "truncate": True},
                )
                resp.raise_for_status()
                vectors: list[list[float]] = resp.json().get("embeddings") or []
                return vectors
            except httpx.HTTPError as exc:
                raise _map_http_error("ollama-embed", exc) from exc

        try:
            vectors = await retry_async(call, max_retries=self._retries, operation="embed")
        except AppError as exc:
            if isinstance(exc, EmbeddingError):
                raise
            raise EmbeddingError(f"Embedding failed: {exc.message}", detail=exc.detail) from exc
        return _validate(vectors, len(inputs), self.dim)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            out.extend(
                await self._embed_batch([self._doc_prefix + t for t in texts[i : i + self._batch]])
            )
        return out

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed_batch([self._query_prefix + text]))[0]


class FakeEmbedder:
    """Deterministic hashed bag-of-words vectors. Lexically similar texts get similar vectors,
    which is enough for tests to check ranking without a model server."""

    def __init__(self, dim: int = 768, model: str = "fake-embed") -> None:
        self.model, self.dim = model, dim
        self.calls = 0

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _TOKEN.findall(text.lower()):
            digest = hashlib.md5(token.encode(), usedforsecurity=False).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            vec[index] += 1.0 if digest[4] % 2 else -1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0:
            vec[0], norm = 1.0, 1.0
        return [x / norm for x in vec]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        return self._vector(text)
