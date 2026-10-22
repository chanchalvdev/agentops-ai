"""Composition root: builds every long-lived service once per process.

Tests build the same container with fakes injected, so the API code under test is identical to
production code.
"""

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import ApiKeyAuthenticator
from app.db.session import make_engine, make_sessionmaker
from app.ingestion.embeddings import Embedder, FakeEmbedder, OllamaEmbedder
from app.ingestion.pipeline import IngestionService
from app.llm.provider import (
    FakeProvider,
    LLMProvider,
    LLMRouter,
    OllamaProvider,
    OpenAICompatibleProvider,
    Route,
)
from app.rag.answer import Answerer
from app.retrieval.hybrid_search import HybridRetriever


@dataclass
class Services:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    http: httpx.AsyncClient
    auth: ApiKeyAuthenticator
    embedder: Embedder
    llm: LLMRouter
    ingestion: IngestionService
    retriever: HybridRetriever
    answerer: Answerer

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.engine.dispose()


def build_router(
    settings: Settings, http: httpx.AsyncClient, primary: LLMProvider | None = None
) -> LLMRouter:
    if primary is None:
        primary = (
            FakeProvider()
            if settings.llm_provider == "fake"
            else OllamaProvider(http, settings.ollama_base_url)
        )
    main = [Route(primary, settings.llm_model_main)]
    small = [Route(primary, settings.llm_model_small)]
    if settings.fallback_enabled:
        assert settings.fallback_api_key and settings.fallback_base_url and settings.fallback_model
        fallback = OpenAICompatibleProvider(
            http, settings.fallback_base_url, settings.fallback_api_key.get_secret_value()
        )
        main.append(Route(fallback, settings.fallback_model))
        small.append(Route(fallback, settings.fallback_model))
    return LLMRouter({"main": main, "small": small}, max_retries=settings.llm_max_retries)


def build_services(
    settings: Settings, *, embedder: Embedder | None = None, llm_provider: LLMProvider | None = None
) -> Services:
    engine = make_engine(settings.database_url)
    sessions = make_sessionmaker(engine)
    http = httpx.AsyncClient(limits=httpx.Limits(max_connections=20))
    if embedder is None:
        embedder = (
            FakeEmbedder(settings.embedding_dim)
            if settings.embedding_provider == "fake"
            else OllamaEmbedder(
                http,
                settings.ollama_base_url,
                settings.embedding_model,
                settings.embedding_dim,
                settings.embedding_batch_size,
            )
        )
    llm = build_router(settings, http, llm_provider)
    retriever = HybridRetriever(
        sessions, embedder, candidate_k=settings.retrieval_candidate_k, rrf_k=settings.rrf_k
    )
    return Services(
        settings=settings,
        engine=engine,
        sessions=sessions,
        http=http,
        auth=ApiKeyAuthenticator(settings),
        embedder=embedder,
        llm=llm,
        ingestion=IngestionService(sessions, embedder, settings),
        retriever=retriever,
        answerer=Answerer(
            retriever,
            llm,
            max_evidence_chars=settings.max_evidence_chars,
            max_output_tokens=settings.llm_max_output_tokens,
            timeout_s=settings.llm_timeout_s,
        ),
    )
