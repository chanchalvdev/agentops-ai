"""The custom loop against real retrieval and persistence (fake model, real Postgres)."""

from collections.abc import Callable
from typing import Any

import pytest

from app.core.budgets import ExecutionLimits
from app.core.errors import NotFoundError
from app.core.execution_context import ExecutionContext
from app.llm.provider import FakeProvider
from app.llm.schemas import LLMRequest, LLMResponse, ToolCall
from app.services import Services

pytestmark = pytest.mark.integration
Ingest = Callable[..., Any]


def agent_script(request: LLMRequest) -> LLMResponse:
    """Search on the first turn, then answer citing S1."""
    turn = sum(1 for m in request.messages if m.role == "assistant")
    if turn == 0:
        calls = [ToolCall(id="c1", name="document_search", arguments={"query": "RPO"})]
    else:
        calls = [
            ToolCall(
                id="f",
                name="final_answer",
                arguments={"answer": "The RPO is 5 minutes [S1].", "citations": ["S1"]},
            )
        ]
    return LLMResponse(
        content="", tool_calls=calls, model=request.model, provider="fake", latency_ms=1
    )


async def test_loop_answers_from_real_retrieval_and_persists_the_ledger(
    services: Services, ingest: Ingest, fake_llm: FakeProvider
) -> None:
    fake_llm._responder = agent_script
    doc = await ingest("dr.md", "# DR\n\nThe Recovery Point Objective (RPO) is 5 minutes.")
    ctx = ExecutionContext(
        tenant_id="tenant_a", limits=ExecutionLimits(), allowed_tools=services.tools.names
    )
    await services.executions.start(ctx, "loop", "What is the RPO?")
    result = await services.agent_loop.run(ctx, "What is the RPO?")
    await services.executions.save(ctx, result)

    assert result.status == "answered"
    assert result.citations[0].document_id == str(doc.id)

    row = await services.executions.get("tenant_a", ctx.execution_id)
    assert row.status == "answered" and row.model_calls == 2 and row.tool_calls == 1
    assert row.citations[0]["filename"] == "dr.md" and row.finished_at is not None
    events = await services.executions.events("tenant_a", ctx.execution_id)
    assert [e.kind for e in events] == [
        "step",
        "model_call",
        "tool_call",
        "step",
        "model_call",
        "final",
    ]
    assert events[2].data["arguments"]["query"] == "RPO"

    await services.executions.save(ctx, result)  # idempotent: no duplicate events
    assert len(await services.executions.events("tenant_a", ctx.execution_id)) == 6

    with pytest.raises(NotFoundError):
        await services.executions.get("tenant_b", ctx.execution_id)
    with pytest.raises(NotFoundError):
        await services.executions.events("tenant_b", ctx.execution_id)
