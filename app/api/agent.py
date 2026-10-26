"""Agent execution, execution traces, and human-in-the-loop approvals."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agents.result import AgentRunResult
from app.agents.service import LimitOverrides, Mode
from app.api.deps import PrincipalDep, ServicesDep

router = APIRouter(prefix="/api/v1", tags=["agent"])


class AgentRunRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    mode: Mode = "graph"
    limits: LimitOverrides = Field(default_factory=LimitOverrides)
    tools: list[str] | None = Field(
        default=None, max_length=20, description="Restrict to these tools (default: all)."
    )


class ExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    request_id: str
    mode: str
    status: str
    question: str
    answer: str | None
    citations: list[dict[str, Any]]
    error_code: str | None
    steps: int
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None
    total_ms: float | None
    limits: dict[str, Any]
    created_at: datetime
    finished_at: datetime | None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    kind: str
    name: str
    status: str
    error_code: str | None
    message: str | None
    latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    data: dict[str, Any]
    at: datetime


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    execution_id: uuid.UUID
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: str
    reason: str | None
    created_at: datetime
    decided_at: datetime | None


class DecisionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/agent/run", response_model=AgentRunResult, summary="Run the agent to completion")
async def run_agent(
    body: AgentRunRequest, request: Request, services: ServicesDep, principal: PrincipalDep
) -> AgentRunResult:
    ctx = services.agents.new_context(
        principal.tenant_id,
        request.state.request_id,
        body.limits,
        body.tools,
        principal.permissions,
    )
    return await services.agents.run(ctx, body.mode, body.question)


@router.post(
    "/agent/run/stream",
    summary="Run the agent, streaming progress as Server-Sent Events",
    description=(
        "Events: `step`, `model_call`, `tool_call`, `correction`, `approval`, `policy` as they "
        "happen; then one `citation` per cited source and a closing `final` with the full result "
        "(or `error`). A `: keep-alive` comment is sent after 15 s without events."
    ),
)
async def run_agent_stream(
    body: AgentRunRequest, request: Request, services: ServicesDep, principal: PrincipalDep
) -> StreamingResponse:
    # Validate and build the context before streaming starts, so bad input is a normal 4xx.
    ctx = services.agents.new_context(
        principal.tenant_id,
        request.state.request_id,
        body.limits,
        body.tools,
        principal.permissions,
    )

    async def events() -> AsyncIterator[str]:
        yield _sse("start", {"execution_id": str(ctx.execution_id), "mode": body.mode})
        async for kind, data in services.agents.stream(ctx, body.mode, body.question):
            if kind == "heartbeat":
                yield ": keep-alive\n\n"  # SSE comment: keeps proxies from closing idle streams
                continue
            if kind == "error":
                yield _sse("error", data)
                continue
            if kind == "event":
                if data["kind"] != "final":
                    yield _sse(data["kind"], data)
                continue
            for citation in data["citations"]:
                yield _sse("citation", citation)
            data.pop("ledger", None)  # already streamed event by event
            yield _sse("final", data)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/executions/{execution_id}", response_model=ExecutionOut)
async def get_execution(
    execution_id: uuid.UUID, services: ServicesDep, principal: PrincipalDep
) -> Any:
    return await services.executions.get(principal.tenant_id, execution_id)


@router.get(
    "/executions/{execution_id}/trace",
    response_model=list[EventOut],
    summary="The execution ledger: every model call, tool call, rejection, and decision",
)
async def get_trace(execution_id: uuid.UUID, services: ServicesDep, principal: PrincipalDep) -> Any:
    return await services.executions.events(principal.tenant_id, execution_id)


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    services: ServicesDep,
    principal: PrincipalDep,
    status: Literal["pending", "approved", "rejected", "all"] = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=200),
) -> Any:
    return await services.approvals.list(
        principal.tenant_id, None if status == "all" else status, limit
    )


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=AgentRunResult,
    summary="Approve a paused tool call and resume the execution from its checkpoint",
)
async def approve(
    approval_id: uuid.UUID,
    services: ServicesDep,
    principal: PrincipalDep,
    body: DecisionRequest | None = None,
) -> AgentRunResult:
    reason = body.reason if body else None
    return await services.agents.decide(
        principal.tenant_id,
        approval_id,
        approved=True,
        reason=reason,
        permissions=principal.permissions,
    )


@router.post(
    "/approvals/{approval_id}/reject",
    response_model=AgentRunResult,
    summary="Reject a paused tool call; the run resumes without executing it",
)
async def reject(
    approval_id: uuid.UUID,
    services: ServicesDep,
    principal: PrincipalDep,
    body: DecisionRequest | None = None,
) -> AgentRunResult:
    reason = body.reason if body else None
    return await services.agents.decide(
        principal.tenant_id,
        approval_id,
        approved=False,
        reason=reason,
        permissions=principal.permissions,
    )
