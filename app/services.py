"""Composition root: builds every long-lived service once per process.

Tests build the same container with fakes injected, so the API code under test is identical to
production code.
"""

import asyncio
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
from app.agents.service import AgentService
from app.agents.store import ApprovalStore, ExecutionStore
from app.core.budgets import ExecutionLimits
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
from app.observability.interfaces import TraceSink
from app.observability.langfuse_adapter import LangfuseSink
from app.observability.langsmith_adapter import LangSmithSink
from app.observability.telemetry import LogSink, Telemetry
from app.rag.answer import Answerer
from app.retrieval.hybrid_search import HybridRetriever
from app.tools.calculator import Calculator
from app.tools.document_metadata import DocumentMetadata
from app.tools.document_search import DocumentSearch
from app.tools.registry import ToolRegistry
from app.tools.workspace import workspace_tools
from app.workspace.audit import DbAudit
from app.workspace.jail import WorkspaceManager


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
    approvals: ApprovalStore
    agents: AgentService
    workspaces: WorkspaceManager
    workspace_audit: DbAudit
    telemetry: Telemetry
    eval_task: "asyncio.Task[Any] | None" = None  # the one evaluation run allowed at a time
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
            try:
                await postgres_saver.setup()  # idempotent; manages its own schema versions
            except Exception:
                await self._pool.close()
                self._pool = None
                raise
            saver = postgres_saver
        else:
            saver = InMemorySaver()
        self._graph = GraphRunner(
            self.harness, saver, max_output_tokens=self.settings.llm_max_output_tokens
        )

    async def aclose(self) -> None:
        if self.eval_task is not None and not self.eval_task.done():
            self.eval_task.cancel()  # the run row is marked failed by the runner's finally
            await asyncio.gather(self.eval_task, return_exceptions=True)
        await self.telemetry.flush()  # pending trace exports get a bounded chance to finish
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


def build_telemetry(settings: Settings, http: httpx.AsyncClient) -> Telemetry:
    sinks: list[TraceSink] = [LogSink()] if settings.telemetry_log else []
    if settings.langfuse_public_key and settings.langfuse_secret_key:
        sinks.append(
            LangfuseSink(
                http,
                settings.langfuse_host,
                settings.langfuse_public_key,
                settings.langfuse_secret_key,
            )
        )
    if settings.langsmith_api_key:
        sinks.append(
            LangSmithSink(
                http,
                settings.langsmith_endpoint,
                settings.langsmith_api_key,
                settings.langsmith_project,
                settings.langsmith_scope,
            )
        )
    return Telemetry(
        sinks, capture_io=settings.telemetry_capture_io, timeout_s=settings.telemetry_timeout_s
    )


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
    workspace_audit = DbAudit(sessions)
    workspaces = WorkspaceManager(
        settings.workspace_root,
        workspace_audit,
        max_read_bytes=settings.workspace_max_read_bytes,
        max_write_bytes=settings.workspace_max_write_bytes,
        exec_timeout_s=settings.workspace_exec_timeout_s,
        max_output_bytes=settings.workspace_max_output_bytes,
    )
    tools = ToolRegistry(
        [
            DocumentSearch(retriever),
            Calculator(),
            DocumentMetadata(ingestion),
            *workspace_tools(workspaces),
        ]
    )
    harness = Harness(llm, tools)
    agent_loop = AgentLoop(
        harness,
        max_context_chars=settings.agent_max_context_chars,
        max_output_tokens=settings.llm_max_output_tokens,
    )
    executions, approvals = ExecutionStore(sessions), ApprovalStore(sessions)
    telemetry = build_telemetry(settings, http)
    services: Services
    agents = AgentService(
        agent_loop,
        lambda: services.graph,  # the graph exists only after the async start()
        tools,
        executions,
        approvals,
        ExecutionLimits.from_settings(settings),
        telemetry,
    )
    services = Services(
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
        agent_loop=agent_loop,
        executions=executions,
        approvals=approvals,
        agents=agents,
        workspaces=workspaces,
        workspace_audit=workspace_audit,
        telemetry=telemetry,
    )
    return services
