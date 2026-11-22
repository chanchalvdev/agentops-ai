"""Embedding providers.

Documents and queries are embedded differently on purpose: nomic-embed-text is trained with task
prefixes ("search_document: " / "search_query: ") and retrieval quality drops without them.
"""

import asyncio
import hashlib
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

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


class FastEmbedEmbedder:
    """In-process CPU embeddings (ONNX via fastembed): no model server and no vendor API.

    The default nomic-embed-text v1.5 is the model Ollama serves as `nomic-embed-text`, so it keeps
    the 768-dim schema and the same task prefixes. The model loads lazily on first use (a few
    seconds; downloaded once into cache_dir unless baked into the image) and inference runs in a
    worker thread so it never blocks the event loop. One lock: ONNX already uses every core.
    """

    def __init__(
        self,
        model: str,
        dim: int,
        batch_size: int = 32,
        *,
        cache_dir: Path | None = None,
        model_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.model, self.dim, self._batch = model, dim, batch_size
        self._cache_dir = cache_dir
        self._factory = model_factory or self._load
        self._impl: Any = None
        self._lock = asyncio.Lock()
        nomic = "nomic" in model
        self._doc_prefix = "search_document: " if nomic else ""
        self._query_prefix = "search_query: " if nomic else ""

    def _load(self) -> Any:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise EmbeddingError(
                "embedding_provider=fastembed needs the 'hosted' extra: uv sync --extra hosted"
            ) from exc
        cache = str(self._cache_dir) if self._cache_dir else None
        return TextEmbedding(model_name=self.model, cache_dir=cache)

    def _run(self, inputs: list[str]) -> list[list[float]]:
        if self._impl is None:
            self._impl = self._factory()
        return [[float(x) for x in vec] for vec in self._impl.embed(inputs, batch_size=self._batch)]

    async def _embed(self, inputs: list[str]) -> list[list[float]]:
        async with self._lock:
            try:
                vectors = await asyncio.to_thread(self._run, inputs)
            except EmbeddingError:
                raise
            except Exception as exc:
                raise EmbeddingError("Embedding failed", detail=repr(exc)) from exc
        return _validate(vectors, len(inputs), self.dim)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed([self._doc_prefix + t for t in texts]) if texts else []

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([self._query_prefix + text]))[0]


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
