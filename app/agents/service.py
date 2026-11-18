"""Entry point for agent executions: create the context, run loop or graph, always persist.

Persistence happens on every exit path, including client disconnects (CancelledError) and
unexpected exceptions, so no execution row is left in "running".
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from app.agents.custom_loop import AgentLoop
from app.agents.evidence import EvidenceStore, FinalAnswer, ToolReference
from app.agents.graph import GraphRunner
from app.agents.result import AgentRunResult, build_result
from app.agents.store import ApprovalStore, ExecutionStore
from app.core.budgets import ExecutionLimits
from app.core.errors import AppError, ConflictError, InvalidRequestError, NotFoundError
from app.core.execution_context import DEFAULT_PERMISSIONS, ExecutionContext
from app.rag.answer import Citation
from app.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from app.agents.semantic_cache import CachedAnswer, SemanticCache
    from app.observability.telemetry import Telemetry

logger = logging.getLogger(__name__)
Mode = Literal["loop", "graph"]


class LimitOverrides(BaseModel):
    """Per-request limits. Each may only tighten the server default."""

    max_steps: int | None = Field(default=None, ge=1)
    max_graph_steps: int | None = Field(default=None, ge=1)
    max_model_calls: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_total_tokens: int | None = Field(default=None, ge=1)
    deadline_s: float | None = Field(default=None, gt=0)


class _LiveRun:
    """A streamed run in this process, followable by any number of SSE connections."""

    def __init__(self, ctx: ExecutionContext, task: "asyncio.Task[AgentRunResult]") -> None:
        self.ctx, self.task = ctx, task
        self.wakers: set[asyncio.Event] = set()

    def wake(self) -> None:
        for event in self.wakers:
            event.set()

    async def stop(self) -> None:
        if not self.task.done():
            self.ctx.cancel("client disconnected")
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def stop_unless_followed(self, grace_s: float) -> None:
        await asyncio.sleep(grace_s)
        if not self.wakers:
            await self.stop()


class AgentService:
    def __init__(
        self,
        loop: AgentLoop,
        graph: Callable[[], GraphRunner],
        tools: ToolRegistry,
        executions: ExecutionStore,
        approvals: ApprovalStore,
        default_limits: ExecutionLimits,
        telemetry: "Telemetry | None" = None,
        semantic_cache: "SemanticCache | None" = None,
    ) -> None:
        self._loop, self._graph, self._tools = loop, graph, tools
        self.executions, self.approvals = executions, approvals
        self._defaults = default_limits
        self._telemetry = telemetry
        self.semantic_cache = semantic_cache
        self._resumes: set[asyncio.Task[AgentRunResult]] = set()
        self._live: dict[uuid.UUID, _LiveRun] = {}
        self._reapers: set[asyncio.Task[None]] = set()

    def new_context(
        self,
        tenant_id: str,
        request_id: str,
        overrides: LimitOverrides | None = None,
        allowed_tools: list[str] | None = None,
        permissions: frozenset[str] = DEFAULT_PERMISSIONS,
    ) -> ExecutionContext:
        if allowed_tools is None:
            # By default the model is offered only the tools this tenant may actually use.
            names = frozenset(
                name
                for name in self._tools.names
                if (tool := self._tools.get(name)) and tool.permissions <= permissions
            )
        else:
            unknown = set(allowed_tools) - self._tools.names
            if unknown:
                raise InvalidRequestError(f"Unknown tools: {sorted(unknown)}")
            names = frozenset(allowed_tools)  # still permission-checked on every call
        return ExecutionContext(
            tenant_id=tenant_id,
            request_id=request_id,
            limits=self._defaults.tightened(**(overrides or LimitOverrides()).model_dump()),
            allowed_tools=names,
            permissions=permissions,
        )

    async def run(self, ctx: ExecutionContext, mode: Mode, question: str) -> AgentRunResult:
        await self.executions.start(ctx, mode, question)
        cache = self.semantic_cache
        lookup = None
        if cache is not None:
            try:
                lookup = await cache.lookup(ctx, mode, question)
            except Exception:  # a cache outage only costs the saving, never the answer
                logger.warning("semantic cache lookup failed", exc_info=True)
        if lookup is not None and lookup[0] is not None:
            hit = lookup[0]
            return await self._execute(
                ctx, mode, lambda: self._from_cache(ctx, mode, hit), question=question
            )
        result = await self._execute(
            ctx, mode, lambda: self._start(ctx, mode, question), question=question
        )
        if cache is not None and lookup is not None:
            cache.store(lookup[1], lookup[2], question, result)
        return result

    async def _from_cache(
        self, ctx: ExecutionContext, mode: Mode, hit: "CachedAnswer"
    ) -> AgentRunResult:
        ctx.record(
            "cache",
            "semantic_cache",
            "ok",
            data={
                "similarity": hit.similarity,
                "source_execution_id": hit.source_execution_id,
            },
        )
        ctx.workflow_state = "answered"
        result = build_result(
            ctx, mode, EvidenceStore(), "answered", final=FinalAnswer(answer=hit.answer)
        )
        return result.model_copy(
            update={
                "citations": [Citation.model_validate(c) for c in hit.citations],
                "tool_references": [ToolReference.model_validate(t) for t in hit.tool_references],
                "verification": "supported",  # verified when it was first answered
            }
        )

    async def stream(
        self,
        ctx: ExecutionContext,
        mode: Mode,
        question: str,
        heartbeat_s: float = 15.0,
        reconnect_grace_s: float = 0.0,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Start a run and follow it: ("event", ledger event) as it progresses, ("heartbeat",
        {}) when nothing happened for `heartbeat_s` (a single local model call can take over a
        minute, longer than common proxy idle timeouts), then ("result", result) or ("error",
        error). If the client goes away, the run keeps going for `reconnect_grace_s` so the
        client can reconnect with follow(); with no follower by then it is cancelled."""
        task = asyncio.create_task(self.run(ctx, mode, question))
        live = _LiveRun(ctx, task)
        self._live[ctx.execution_id] = live
        ctx.listeners.append(lambda _event: live.wake())
        task.add_done_callback(lambda _t: live.wake())
        task.add_done_callback(lambda _t: self._live.pop(ctx.execution_id, None))
        # aclosing: when the client disconnects, the inner follower must close right away (its
        # finally decides whether the run stops), not whenever it is garbage-collected.
        async with contextlib.aclosing(
            self._follow(live, -1, heartbeat_s, reconnect_grace_s)
        ) as follower:
            async for item in follower:
                yield item

    def live_run(self, tenant_id: str, execution_id: uuid.UUID) -> "_LiveRun | None":
        live = self._live.get(execution_id)
        return live if live is not None and live.ctx.tenant_id == tenant_id else None

    async def follow(
        self,
        tenant_id: str,
        execution_id: uuid.UUID,
        after_seq: int,
        heartbeat_s: float = 15.0,
        reconnect_grace_s: float = 0.0,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Reattach to a run started by stream() in this process, replaying every ledger event
        after `after_seq` (the SSE Last-Event-ID). Raises NotFoundError if it is not live here
        (it finished, or runs in another process): read the stored execution instead."""
        live = self.live_run(tenant_id, execution_id)
        if live is None:
            raise NotFoundError("Execution is not running in this process")
        async with contextlib.aclosing(
            self._follow(live, after_seq, heartbeat_s, reconnect_grace_s)
        ) as follower:
            async for item in follower:
                yield item

    async def _follow(
        self, live: "_LiveRun", after_seq: int, heartbeat_s: float, grace_s: float
    ) -> AsyncGenerator[tuple[str, dict[str, Any]]]:
        wake = asyncio.Event()
        live.wakers.add(wake)
        pos = after_seq + 1
        try:
            while True:
                wake.clear()  # before draining: an event appended meanwhile sets it again
                while pos < len(live.ctx.ledger):
                    yield "event", live.ctx.ledger[pos].model_dump(mode="json")
                    pos += 1
                if live.task.done():
                    break
                try:
                    await asyncio.wait_for(wake.wait(), timeout=heartbeat_s)
                except TimeoutError:
                    yield "heartbeat", {}
            exc = live.task.exception() if not live.task.cancelled() else None
            if live.task.cancelled() or exc is not None:
                if exc is not None:
                    logger.error("streamed execution failed", exc_info=exc)
                code = exc.code if isinstance(exc, AppError) else "internal_error"
                if live.task.cancelled():
                    code = "execution_cancelled"
                yield "error", {"code": code, "execution_id": str(live.ctx.execution_id)}
                return
            yield "result", live.task.result().model_dump(mode="json")
        finally:
            live.wakers.discard(wake)
            if not live.task.done() and not live.wakers:
                if grace_s <= 0:  # nobody can reconnect: stop now; run() persists "cancelled"
                    await live.stop()
                else:
                    self._reapers.add(t := asyncio.create_task(live.stop_unless_followed(grace_s)))
                    t.add_done_callback(self._reapers.discard)

    async def maintain(self, ttl_s: float, resume_grace_s: float) -> None:
        """Housekeeping, run when approvals are listed or decided: expire approvals nobody
        decided, and reopen decisions whose resume died with its process."""
        expired = await self.approvals.expire(ttl_s)
        if expired:
            logger.warning("approvals expired", extra={"executions": [str(i) for i in expired]})
        for execution_id in expired:
            with contextlib.suppress(Exception):
                await self._graph().forget(execution_id)
        if reopened := await self.approvals.recover_stuck(resume_grace_s):
            logger.warning("decided approvals reopened after a lost resume", extra={"n": reopened})

    async def claim(
        self,
        tenant_id: str,
        approval_id: uuid.UUID,
        *,
        approved: bool,
        reason: str | None,
        permissions: frozenset[str] | None = None,
    ) -> tuple[ExecutionContext, Callable[[], Any], str]:
        """Claim the decision and prepare the resume. If anything fails before the resume
        starts, the claim is released, so the approval never stays "approved" with its run
        still paused."""
        approval = await self.approvals.decide(
            tenant_id, approval_id, approved=approved, reason=reason
        )
        try:
            graph = self._graph()
            ctx = await graph.restore_context(approval.execution_id)
            if ctx is None or ctx.tenant_id != tenant_id:
                raise NotFoundError("Execution not found")
            if permissions is not None:
                # Permissions are snapshotted in the checkpoint; a grant revoked while the run
                # was paused must not still let the approved write through. Tighten only.
                ctx.permissions = ctx.permissions & permissions
            execution = await self.executions.get(tenant_id, ctx.execution_id)
            if execution.status != "awaiting_approval":
                raise ConflictError(f"Execution is {execution.status}, not awaiting approval")
        except BaseException:
            await asyncio.shield(self.approvals.release(approval_id, "resume could not start"))
            raise

        def resume() -> Any:
            return graph.resume(ctx, approved=approved, reason=reason, call_id=approval.call_id)

        return ctx, resume, execution.question

    async def decide(
        self,
        tenant_id: str,
        approval_id: uuid.UUID,
        *,
        approved: bool,
        reason: str | None,
        permissions: frozenset[str] | None = None,
    ) -> AgentRunResult:
        """Decide and wait for the resumed run to finish (CLI, evaluation runner)."""
        ctx, resume, question = await self.claim(
            tenant_id, approval_id, approved=approved, reason=reason, permissions=permissions
        )
        # question: so the finished trace still carries its input
        return await self._execute(ctx, "graph", resume, question=question)

    async def decide_in_background(
        self,
        tenant_id: str,
        approval_id: uuid.UUID,
        *,
        approved: bool,
        reason: str | None,
        permissions: frozenset[str] | None = None,
    ) -> uuid.UUID:
        """Decide, then resume without holding the caller's request open (the resumed run
        can take minutes on a local model). Returns the execution id to poll."""
        ctx, resume, question = await self.claim(
            tenant_id, approval_id, approved=approved, reason=reason, permissions=permissions
        )
        task = asyncio.create_task(self._execute(ctx, "graph", resume, question=question))
        self._resumes.add(task)
        task.add_done_callback(self._resumes.discard)
        return ctx.execution_id

    async def aclose(self) -> None:
        """Stop in-flight background resumes and streamed runs; each is saved "cancelled"."""
        for task in [*self._resumes, *self._reapers]:
            task.cancel()
        await asyncio.gather(*self._resumes, *self._reapers, return_exceptions=True)
        await asyncio.gather(*(live.stop() for live in list(self._live.values())))

    async def _start(self, ctx: ExecutionContext, mode: Mode, question: str) -> AgentRunResult:
        if mode == "loop":
            return await self._loop.run(ctx, question)
        return await self._graph().run(ctx, question)

    async def _execute(
        self,
        ctx: ExecutionContext,
        mode: Mode,
        run: Callable[[], Any],
        question: str | None = None,
    ) -> AgentRunResult:
        try:
            result: AgentRunResult = await run()
        except asyncio.CancelledError:
            ctx.cancel("cancelled")
            ctx.record("final", mode, "error", error_code="execution_cancelled")
            result = build_result(
                ctx, mode, EvidenceStore(), "cancelled", error_code="execution_cancelled"
            )
            await asyncio.shield(self.executions.save(ctx, result))
            self._trace(ctx, result, question)
            raise
        except ConflictError:
            # Raised before anything ran (e.g. resuming a run that is not paused): there is no
            # new execution activity to persist or trace.
            raise
        except Exception:
            logger.exception(
                "agent execution crashed", extra={"execution_id": str(ctx.execution_id)}
            )
            ctx.record("final", mode, "error", error_code="internal_error")
            result = build_result(ctx, mode, EvidenceStore(), "failed", error_code="internal_error")
        try:
            pending = None
            if result.status == "awaiting_approval":
                pending = await self._graph().pending_approval(ctx)
            await self.executions.save(ctx, result, approval=pending)
            if mode == "graph" and result.status != "awaiting_approval":
                with contextlib.suppress(Exception):  # retention cleanup; never fails the run
                    await self._graph().forget(ctx.execution_id)
        finally:
            # A failed save is exactly when the trace matters most: export it either way.
            self._trace(ctx, result, question)
        return result

    def _trace(self, ctx: ExecutionContext, result: AgentRunResult, question: str | None) -> None:
        if self._telemetry is not None:
            self._telemetry.submit(ctx, result, question)  # background; never fails the run
