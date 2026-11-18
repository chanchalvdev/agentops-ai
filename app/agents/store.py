"""Persistence for executions and their ledger. All reads are scoped by tenant."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.result import AgentRunResult
from app.core.errors import ConflictError, NotFoundError
from app.core.execution_context import ExecutionContext
from app.db.models import AgentExecution, Approval, ExecutionEvent


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

    async def save(
        self,
        ctx: ExecutionContext,
        result: AgentRunResult,
        approval: dict[str, Any] | None = None,
    ) -> None:
        """Write the current outcome and any new ledger events in one transaction.

        Safe to call more than once (a paused graph run saves, then saves again on resume):
        events are keyed by (execution_id, seq) and already-stored ones are skipped. A paused
        run passes its pending tool call as `approval`: the "awaiting_approval" status and the
        approval row are written together, so neither can exist without the other.
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
            if approval is not None:
                await session.execute(_approval_insert(ctx.tenant_id, ctx.execution_id, approval))

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

    async def recent(
        self, tenant_id: str, limit: int = 50, status: str | None = None
    ) -> list[AgentExecution]:
        """The tenant's most recent executions, newest first."""
        stmt = select(AgentExecution).where(AgentExecution.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(AgentExecution.status == status)
        async with self._sessions() as session:
            rows = await session.scalars(
                stmt.order_by(AgentExecution.created_at.desc()).limit(limit)
            )
            return list(rows)

    async def events(self, tenant_id: str, execution_id: uuid.UUID) -> list[ExecutionEvent]:
        await self.get(tenant_id, execution_id)  # ownership check
        async with self._sessions() as session:
            rows = await session.scalars(
                select(ExecutionEvent)
                .where(ExecutionEvent.execution_id == execution_id)
                .order_by(ExecutionEvent.seq)
            )
            return list(rows)


def _approval_insert(tenant_id: str, execution_id: uuid.UUID, payload: dict[str, Any]) -> Any:
    return (
        insert(Approval)
        .values(
            id=uuid.uuid4(),
            execution_id=execution_id,
            tenant_id=tenant_id,
            call_id=payload["call_id"],
            tool_name=payload["tool"],
            arguments=payload["arguments"],
            status="pending",
        )
        # Re-saving the same paused state must not create a second approval.
        .on_conflict_do_update(
            constraint="uq_approvals_execution_call", set_={"call_id": payload["call_id"]}
        )
        .returning(Approval)
    )


class ApprovalStore:
    """Pending human decisions. The pending -> decided transition is a single conditional
    UPDATE, so two concurrent approvals of the same call cannot both resume the run."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self, tenant_id: str, execution_id: uuid.UUID, payload: dict[str, Any]
    ) -> Approval:
        async with self._sessions() as session, session.begin():
            row: Approval | None = await session.scalar(
                _approval_insert(tenant_id, execution_id, payload)
            )
            assert row is not None
            return row

    async def find(self, tenant_id: str, status: str | None, limit: int = 50) -> list[Approval]:
        query = select(Approval).where(Approval.tenant_id == tenant_id)
        if status:
            query = query.where(Approval.status == status)
        async with self._sessions() as session:
            rows = await session.scalars(query.order_by(Approval.created_at.desc()).limit(limit))
            return list(rows)

    async def decide(
        self, tenant_id: str, approval_id: uuid.UUID, *, approved: bool, reason: str | None
    ) -> Approval:
        async with self._sessions() as session, session.begin():
            row: Approval | None = await session.scalar(
                update(Approval)
                .where(
                    Approval.id == approval_id,
                    Approval.tenant_id == tenant_id,
                    Approval.status == "pending",
                )
                .values(
                    status="approved" if approved else "rejected",
                    reason=reason,
                    decided_at=datetime.now(UTC),
                )
                .returning(Approval)
            )
            if row is not None:
                return row
            exists = await session.scalar(
                select(Approval.status).where(
                    Approval.id == approval_id, Approval.tenant_id == tenant_id
                )
            )
        if exists is None:
            raise NotFoundError("Approval not found")
        raise ConflictError(f"Approval already {exists}")

    async def release(self, approval_id: uuid.UUID, note: str) -> None:
        """Undo a claim whose resume never started: the decision can be made again."""
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(Approval)
                .where(Approval.id == approval_id, Approval.status.in_(["approved", "rejected"]))
                .values(status="pending", reason=note, decided_at=None)
            )

    async def expire(self, ttl_s: float) -> list[uuid.UUID]:
        """Pending approvals older than `ttl_s` become "expired" and their paused executions
        "cancelled" (error_code approval_expired), in one transaction. Returns the execution ids
        (their checkpoints can then be deleted)."""
        now = datetime.now(UTC)
        async with self._sessions() as session, session.begin():
            ids = list(
                await session.scalars(
                    update(Approval)
                    .where(
                        Approval.status == "pending",
                        Approval.created_at < now - timedelta(seconds=ttl_s),
                    )
                    .values(
                        status="expired",
                        reason=f"no decision within {ttl_s / 3600:g} h",
                        decided_at=now,
                    )
                    .returning(Approval.execution_id)
                )
            )
            if ids:
                await session.execute(
                    update(AgentExecution)
                    .where(
                        AgentExecution.id.in_(ids),
                        AgentExecution.status == "awaiting_approval",
                    )
                    .values(
                        status="cancelled",
                        error_code="approval_expired",
                        answer="Stopped: no human decision before the approval expired.",
                        finished_at=now,
                    )
                )
        return ids

    async def recover_stuck(self, after_s: float) -> int:
        """Decided approvals whose execution is still "awaiting_approval" long after the
        decision: the resuming process died (crash, restart) before saving any outcome. Put
        them back to pending so a human can decide again. Re-running the approved write is
        safe: write tools are idempotent per (execution, call) through their receipts."""
        now = datetime.now(UTC)
        async with self._sessions() as session, session.begin():
            stuck = (
                select(AgentExecution.id)
                .where(AgentExecution.status == "awaiting_approval")
                .scalar_subquery()
            )
            result = await session.execute(
                update(Approval)
                .where(
                    Approval.status.in_(["approved", "rejected"]),
                    Approval.decided_at < now - timedelta(seconds=after_s),
                    Approval.execution_id.in_(stuck),
                )
                .values(
                    status="pending",
                    reason="the resume after this decision did not complete; decide again",
                    decided_at=None,
                )
            )
        return int(getattr(result, "rowcount", 0) or 0)
