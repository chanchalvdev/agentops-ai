"""The outcome of one agent execution, shared by the custom loop and the LangGraph workflow."""

import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from app.agents.evidence import EvidenceStore, FinalAnswer, ToolReference, cited_labels
from app.core.errors import (
    AppError,
    BudgetExhaustedError,
    DeadlineExceededError,
    ExecutionCancelledError,
)
from app.core.execution_context import ExecutionContext, LedgerEvent
from app.llm.schemas import Usage
from app.rag.answer import Citation

RunStatus = Literal[
    "answered",
    "insufficient_evidence",
    "failed_validation",
    "awaiting_approval",
    "budget_exhausted",
    "deadline_exceeded",
    "cancelled",
    "failed",
]

FALLBACK_TEXT: dict[str, str] = {
    "failed_validation": "I could not produce an answer verifiably grounded in your documents.",
    "awaiting_approval": "Waiting for a human to approve a requested action.",
    "budget_exhausted": "Stopped: the execution budget was used up before an answer was verified.",
    "deadline_exceeded": "Stopped: the execution ran past its deadline.",
    "cancelled": "Stopped: the execution was cancelled.",
    "failed": "The execution failed.",
}


class AgentRunResult(BaseModel):
    execution_id: uuid.UUID
    request_id: str
    mode: Literal["loop", "graph"]
    status: RunStatus
    answer: str
    citations: list[Citation]
    tool_references: list[ToolReference]
    error_code: str | None = None
    steps: int
    model_calls: int
    tool_calls: int
    corrections: dict[str, int]
    usage: Usage
    cost_usd: Decimal | None
    total_ms: float
    ledger: list[LedgerEvent]


def status_for_error(exc: AppError) -> RunStatus:
    if isinstance(exc, BudgetExhaustedError):
        return "budget_exhausted"
    if isinstance(exc, DeadlineExceededError):
        return "deadline_exceeded"
    if isinstance(exc, ExecutionCancelledError):
        return "cancelled"
    return "failed"


def build_result(
    ctx: ExecutionContext,
    mode: Literal["loop", "graph"],
    store: EvidenceStore,
    status: RunStatus,
    final: FinalAnswer | None = None,
    error: AppError | None = None,
) -> AgentRunResult:
    labels = cited_labels(final) if final and status == "answered" else []
    answer = final.answer if final and status in ("answered", "insufficient_evidence") else None
    counters = ctx.counters
    return AgentRunResult(
        execution_id=ctx.execution_id,
        request_id=ctx.request_id,
        mode=mode,
        status=status,
        answer=answer or FALLBACK_TEXT.get(status, ""),
        citations=store.citations(labels),
        tool_references=store.references(labels),
        error_code=error.code if error else None,
        steps=counters.steps,
        model_calls=counters.model_calls,
        tool_calls=counters.tool_calls,
        corrections=dict(counters.corrections),
        usage=counters.usage,
        cost_usd=counters.cost_usd if counters.cost_known else None,
        total_ms=round((ctx.clock() - ctx.started_at) * 1000, 1),
        ledger=list(ctx.ledger),
    )
