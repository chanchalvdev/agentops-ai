"""Persistence for executions and their ledger. All reads are scoped by tenant."""

import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.result import AgentRunResult
from app.core.errors import NotFoundError
from app.core.execution_context import ExecutionContext
from app.db.models import AgentExecution, ExecutionEvent


class ExecutionStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def start(
        self, ctx: ExecutionContext, mode: Literal["loop", "graph"], question: str
    ) -> None:
        async with self._sessions() as session, session.begin():
            session.add(
                AgentExecution(
                    id=ctx.execution_id,
                    tenant_id=ctx.tenant_id,
                    request_id=ctx.request_id,
                    mode=mode,
                    status="running",
                    question=question,
                    citations=[],
                    steps=0,
                    model_calls=0,
                    tool_calls=0,
                    input_tokens=0,
                    output_tokens=0,
                    limits=ctx.limits.model_dump(mode="json"),
                )
            )

    async def save(self, ctx: ExecutionContext, result: AgentRunResult) -> None:
        """Write the current outcome and any new ledger events in one transaction.

        Safe to call more than once (a paused graph run saves, then saves again on resume):
        events are keyed by (execution_id, seq) and already-stored ones are skipped.
        """
        terminal = result.status != "awaiting_approval"
        async with self._sessions() as session, session.begin():
            row = await session.get(AgentExecution, ctx.execution_id, with_for_update=True)
            if row is None or row.tenant_id != ctx.tenant_id:
                raise NotFoundError("Execution not found")
            row.status = result.status
            row.answer = result.answer
            row.citations = [c.model_dump(mode="json") for c in result.citations]
            row.error_code = result.error_code
            row.steps, row.model_calls, row.tool_calls = (
                result.steps,
                result.model_calls,
                result.tool_calls,
            )
            row.input_tokens = result.usage.input_tokens
            row.output_tokens = result.usage.output_tokens
            row.cost_usd = result.cost_usd
            row.total_ms = result.total_ms
            row.finished_at = datetime.now(UTC) if terminal else None
            if ctx.ledger:
                await session.execute(
                    insert(ExecutionEvent)
                    .values(
                        [
                            {"execution_id": ctx.execution_id, **e.model_dump(mode="python")}
                            for e in ctx.ledger
                        ]
                    )
                    .on_conflict_do_nothing(constraint="uq_events_execution_seq")
                )

    async def get(self, tenant_id: str, execution_id: uuid.UUID) -> AgentExecution:
        async with self._sessions() as session:
            row = await session.scalar(
                select(AgentExecution).where(
                    AgentExecution.id == execution_id, AgentExecution.tenant_id == tenant_id
                )
            )
        if row is None:
            raise NotFoundError("Execution not found")
        return row

    async def events(self, tenant_id: str, execution_id: uuid.UUID) -> list[ExecutionEvent]:
        await self.get(tenant_id, execution_id)  # ownership check
        async with self._sessions() as session:
            rows = await session.scalars(
                select(ExecutionEvent)
                .where(ExecutionEvent.execution_id == execution_id)
                .order_by(ExecutionEvent.seq)
            )
            return list(rows)
