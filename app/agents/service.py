"""Entry point for agent executions: create the context, run loop or graph, always persist.

Persistence happens on every exit path, including client disconnects (CancelledError) and
unexpected exceptions, so no execution row is left in "running".
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.custom_loop import AgentLoop
from app.agents.evidence import EvidenceStore
from app.agents.graph import GraphRunner
from app.agents.result import AgentRunResult, build_result
from app.agents.store import ApprovalStore, ExecutionStore
from app.core.budgets import ExecutionLimits
from app.core.errors import AppError, ConflictError, InvalidRequestError, NotFoundError
from app.core.execution_context import ExecutionContext, LedgerEvent
from app.tools.registry import ToolRegistry

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


class AgentService:
    def __init__(
        self,
        loop: AgentLoop,
        graph: Callable[[], GraphRunner],
        tools: ToolRegistry,
        executions: ExecutionStore,
        approvals: ApprovalStore,
        default_limits: ExecutionLimits,
    ) -> None:
        self._loop, self._graph, self._tools = loop, graph, tools
        self.executions, self.approvals = executions, approvals
        self._defaults = default_limits

    def new_context(
        self,
        tenant_id: str,
        request_id: str,
        overrides: LimitOverrides | None = None,
        allowed_tools: list[str] | None = None,
    ) -> ExecutionContext:
        names = self._tools.names
        if allowed_tools is not None:
            unknown = set(allowed_tools) - names
            if unknown:
                raise InvalidRequestError(f"Unknown tools: {sorted(unknown)}")
            names = frozenset(allowed_tools)
        return ExecutionContext(
            tenant_id=tenant_id,
            request_id=request_id,
            limits=self._defaults.tightened(**(overrides or LimitOverrides()).model_dump()),
            allowed_tools=names,
        )

    async def run(self, ctx: ExecutionContext, mode: Mode, question: str) -> AgentRunResult:
        await self.executions.start(ctx, mode, question)
        return await self._execute(ctx, mode, lambda: self._start(ctx, mode, question))

    async def stream(
        self, ctx: ExecutionContext, mode: Mode, question: str, heartbeat_s: float = 15.0
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield ("event", ledger event) as the run progresses, ("heartbeat", {}) when nothing
        happened for `heartbeat_s` (a single local model call can take over a minute, longer
        than common proxy idle timeouts), then ("result", result) or ("error", error)."""
        queue: asyncio.Queue[LedgerEvent] = asyncio.Queue()
        ctx.listeners.append(queue.put_nowait)
        task = asyncio.create_task(self.run(ctx, mode, question))
        try:
            while not task.done() or not queue.empty():
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait(
                    {getter, task}, timeout=heartbeat_s, return_when=asyncio.FIRST_COMPLETED
                )
                if getter in done:
                    yield "event", getter.result().model_dump(mode="json")
                    continue
                getter.cancel()
                if not done:
                    yield "heartbeat", {}
            if task.exception() is not None:
                exc = task.exception()
                logger.error("streamed execution failed", exc_info=exc)
                code = exc.code if isinstance(exc, AppError) else "internal_error"
                yield "error", {"code": code, "execution_id": str(ctx.execution_id)}
                return
            yield "result", task.result().model_dump(mode="json")
        finally:
            if not task.done():  # client went away: stop the run; run() persists "cancelled"
                ctx.cancel("client disconnected")
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def decide(
        self, tenant_id: str, approval_id: uuid.UUID, *, approved: bool, reason: str | None
    ) -> AgentRunResult:
        approval = await self.approvals.decide(
            tenant_id, approval_id, approved=approved, reason=reason
        )
        graph = self._graph()
        ctx = await graph.restore_context(approval.execution_id)
        if ctx is None or ctx.tenant_id != tenant_id:
            raise NotFoundError("Execution not found")
        return await self._execute(
            ctx,
            "graph",
            lambda: graph.resume(ctx, approved=approved, reason=reason, call_id=approval.call_id),
        )

    async def _start(self, ctx: ExecutionContext, mode: Mode, question: str) -> AgentRunResult:
        if mode == "loop":
            return await self._loop.run(ctx, question)
        return await self._graph().run(ctx, question)

    async def _execute(
        self, ctx: ExecutionContext, mode: Mode, run: Callable[[], Any]
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
            raise
        except ConflictError:
            raise
        except Exception:
            logger.exception(
                "agent execution crashed", extra={"execution_id": str(ctx.execution_id)}
            )
            ctx.record("final", mode, "error", error_code="internal_error")
            result = build_result(ctx, mode, EvidenceStore(), "failed", error_code="internal_error")
        await self.executions.save(ctx, result)
        if result.status == "awaiting_approval":
            pending = await self._graph().pending_approval(ctx)
            if pending:
                await self.approvals.create(ctx.tenant_id, ctx.execution_id, pending)
        return result
