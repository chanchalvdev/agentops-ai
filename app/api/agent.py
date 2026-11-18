"""Agent execution, execution traces, and human-in-the-loop approvals."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agents.result import AgentRunResult
from app.agents.service import LimitOverrides, Mode
from app.api.deps import PrincipalDep, ServicesDep, rate_limited

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


def _sse(event: str, data: dict[str, Any], event_id: str | int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _sse_body(
    items: AsyncIterator[tuple[str, dict[str, Any]]], execution_id: uuid.UUID, mode: str
) -> AsyncIterator[str]:
    """Ledger events carry `id: <seq>`, so a client that loses the connection reconnects with
    Last-Event-ID and receives only what it missed. The closing events carry `id: end`."""

    async def body() -> AsyncIterator[str]:
        yield _sse("start", {"execution_id": str(execution_id), "mode": mode})
        async for kind, data in items:
            if kind == "heartbeat":
                yield ": keep-alive\n\n"  # SSE comment: keeps proxies from closing idle streams
                continue
            if kind == "error":
                yield _sse("error", data, "end")
                continue
            if kind == "event":
                if data["kind"] != "final":
                    yield _sse(data["kind"], data, data["seq"])
                continue
            for citation in data["citations"]:
                yield _sse("citation", citation, "end")
            data.pop("ledger", None)  # already streamed event by event
            yield _sse("final", data, "end")

    return body()


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.post(
    "/agent/run",
    response_model=AgentRunResult,
    summary="Run the agent to completion",
    dependencies=[rate_limited("agent")],
)
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
    dependencies=[rate_limited("agent")],
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

    items = services.agents.stream(
        ctx,
        body.mode,
        body.question,
        reconnect_grace_s=services.settings.sse_reconnect_grace_s,
    )
    return StreamingResponse(
        _sse_body(items, ctx.execution_id, body.mode),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get(
    "/executions/{execution_id}/stream",
    summary="Reconnect to a streamed run (send Last-Event-ID to skip what you already have)",
    description=(
        "While the run is live in this server process, replays the ledger events after "
        "Last-Event-ID and continues live. Once it has finished (or lives in another process), "
        "replays the stored events and closes with the stored outcome."
    ),
)
async def follow_execution(
    execution_id: uuid.UUID,
    request: Request,
    services: ServicesDep,
    principal: PrincipalDep,
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else -1
    if last_event_id == "end":
        after = 1 << 30  # the client has everything but the closing events
    live = services.agents.live_run(principal.tenant_id, execution_id)
    if live is not None:
        items = services.agents.follow(
            principal.tenant_id,
            execution_id,
            after,
            reconnect_grace_s=services.settings.sse_reconnect_grace_s,
        )
        mode = "graph"
    else:
        row = await services.executions.get(principal.tenant_id, execution_id)  # 404 if not yours
        items = _stored_items(services, row, after)
        mode = row.mode
    return StreamingResponse(
        _sse_body(items, execution_id, mode), media_type="text/event-stream", headers=_SSE_HEADERS
    )


async def _stored_items(
    services: Any, row: Any, after: int
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    for e in await services.executions.events(row.tenant_id, row.id):
        if e.seq > after:
            yield "event", EventOut.model_validate(e).model_dump(mode="json")
    if row.status == "running":  # live in another process, or its process died
        yield "error", {"code": "execution_not_live_here", "execution_id": str(row.id)}
        return
    yield (
        "result",
        {
            "execution_id": str(row.id),
            "request_id": row.request_id,
            "mode": row.mode,
            "status": row.status,
            "answer": row.answer or "",
            "citations": row.citations,
            "tool_references": [],
            "error_code": row.error_code,
            "verification": None,
            "steps": row.steps,
            "model_calls": row.model_calls,
            "tool_calls": row.tool_calls,
            "usage": {"input_tokens": row.input_tokens, "output_tokens": row.output_tokens},
            "total_ms": row.total_ms,
            "replayed": True,
        },
    )


@router.get(
    "/executions",
    response_model=list[ExecutionOut],
    summary="The caller's most recent executions, newest first",
)
async def list_executions(
    services: ServicesDep,
    principal: PrincipalDep,
    limit: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None, max_length=32),
) -> Any:
    return await services.executions.recent(principal.tenant_id, limit, status)


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
    status: Literal["pending", "approved", "rejected", "expired", "all"] = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=200),
) -> Any:
    await _maintain(services)
    return await services.approvals.find(
        principal.tenant_id, None if status == "all" else status, limit
    )


async def _maintain(services: Any) -> None:
    await services.agents.maintain(
        services.settings.approval_ttl_s, services.settings.approval_resume_grace_s
    )


class DecisionAccepted(BaseModel):
    approval_id: uuid.UUID
    execution_id: uuid.UUID
    status: Literal["resuming"] = "resuming"


async def _decide(
    approval_id: uuid.UUID,
    services: Any,
    principal: Any,
    body: "DecisionRequest | None",
    *,
    approved: bool,
    wait: bool,
) -> Any:
    await _maintain(services)
    reason = body.reason if body else None
    kwargs = {"approved": approved, "reason": reason, "permissions": principal.permissions}
    if wait:
        return await services.agents.decide(principal.tenant_id, approval_id, **kwargs)
    execution_id = await services.agents.decide_in_background(
        principal.tenant_id, approval_id, **kwargs
    )
    return JSONResponse(
        status_code=202,
        content=DecisionAccepted(approval_id=approval_id, execution_id=execution_id).model_dump(
            mode="json"
        ),
        headers={"Location": f"/api/v1/executions/{execution_id}"},
    )


_DECISION_RESPONSES: dict[int | str, dict[str, Any]] = {
    202: {
        "model": DecisionAccepted,
        "description": "Decision recorded; the run resumes in the background. Poll the "
        "execution (Location header) until its status is no longer awaiting_approval.",
    }
}


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=AgentRunResult,
    responses=_DECISION_RESPONSES,
    summary="Approve a paused tool call; the run resumes from its checkpoint",
    dependencies=[rate_limited("agent")],
)
async def approve(
    approval_id: uuid.UUID,
    services: ServicesDep,
    principal: PrincipalDep,
    body: DecisionRequest | None = None,
    wait: bool = Query(default=False, description="true: block until the resumed run ends"),
) -> Any:
    return await _decide(approval_id, services, principal, body, approved=True, wait=wait)


@router.post(
    "/approvals/{approval_id}/reject",
    response_model=AgentRunResult,
    responses=_DECISION_RESPONSES,
    summary="Reject a paused tool call; the run resumes without executing it",
    dependencies=[rate_limited("agent")],
)
async def reject(
    approval_id: uuid.UUID,
    services: ServicesDep,
    principal: PrincipalDep,
    body: DecisionRequest | None = None,
    wait: bool = Query(default=False, description="true: block until the resumed run ends"),
) -> Any:
    return await _decide(approval_id, services, principal, body, approved=False, wait=wait)
