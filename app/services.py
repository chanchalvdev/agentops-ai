"""Composition root: builds every long-lived service once per process.

Tests build the same container with fakes injected, so the API code under test is identical to
production code.
"""

from dataclasses import dataclass
from typing import Any

import httpx
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agents.custom_loop import AgentLoop
from app.agents.graph import GraphRunner
from app.agents.harness import Harness
from app.agents.store import ExecutionStore
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
from app.tools.calculator import Calculator
from app.tools.document_metadata import DocumentMetadata
from app.tools.document_search import DocumentSearch
from app.tools.registry import ToolRegistry


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
    tools: ToolRegistry
    harness: Harness
    agent_loop: AgentLoop
    executions: ExecutionStore
    _graph: GraphRunner | None = None
    _pool: AsyncConnectionPool[Any] | None = None

    @property
    def graph(self) -> GraphRunner:
        if self._graph is None:
            raise RuntimeError("Services.start() has not been awaited")
        return self._graph

    async def start(self) -> None:
        """Open async resources that cannot be created in a sync constructor."""
        if self._graph is not None:
            return
        saver: BaseCheckpointSaver[Any]
        if self.settings.agent_checkpointer == "postgres":
            # The checkpointer speaks psycopg 3, not asyncpg; same database, its own tables.
            conninfo = self.settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
            self._pool = AsyncConnectionPool(
                conninfo,
                max_size=5,
                open=False,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            )
            await self._pool.open()
            postgres_saver = AsyncPostgresSaver(self._pool)
            await postgres_saver.setup()  # idempotent; manages its own schema versions
            saver = postgres_saver
        else:
            saver = InMemorySaver()
        self._graph = GraphRunner(
            self.harness, saver, max_output_tokens=self.settings.llm_max_output_tokens
        )

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
        await self.http.aclose()
        await self.engine.dispose()


def build_router(
    settings: Settings, http: httpx.AsyncClient, primary: LLMProvider | None = None
) -> LLMRouter:
    if primary is None:
        primary = (
            FakeProvider()
            if settings.llm_provider == "fake"
            else OllamaProvider(http, settings.ollama_base_url, settings.llm_num_ctx)
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
    ingestion = IngestionService(sessions, embedder, settings)
    tools = ToolRegistry([DocumentSearch(retriever), Calculator(), DocumentMetadata(ingestion)])
    harness = Harness(llm, tools)
    return Services(
        settings=settings,
        engine=engine,
        sessions=sessions,
        http=http,
        auth=ApiKeyAuthenticator(settings),
        embedder=embedder,
        llm=llm,
        ingestion=ingestion,
        retriever=retriever,
        answerer=Answerer(
            retriever,
            llm,
            max_evidence_chars=settings.max_evidence_chars,
            max_output_tokens=settings.llm_max_output_tokens,
            timeout_s=settings.llm_timeout_s,
        ),
        tools=tools,
        harness=harness,
        agent_loop=AgentLoop(
            harness,
            max_context_chars=settings.agent_max_context_chars,
            max_output_tokens=settings.llm_max_output_tokens,
        ),
        executions=ExecutionStore(sessions),
    )
