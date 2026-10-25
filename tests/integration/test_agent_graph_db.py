"""The LangGraph workflow on the real Postgres checkpointer: a paused run survives a 'restart'."""

from collections.abc import Callable
from typing import Any

import pytest

from app.agents.graph import GraphRunner
from app.core.budgets import ExecutionLimits
from app.core.execution_context import ExecutionContext
from app.llm.provider import FakeProvider
from app.llm.schemas import ToolCall
from app.services import Services
from tests.unit.test_agent_graph import GraphModel, SaveNote

pytestmark = pytest.mark.integration
Ingest = Callable[..., Any]


async def test_graph_pauses_persists_and_resumes_after_restart(
    services: Services, ingest: Ingest, fake_llm: FakeProvider
) -> None:
    await ingest("dr.md", "# DR\n\nThe Recovery Point Objective (RPO) is 5 minutes.")
    note = SaveNote()
    services.tools.register(note)
    fake_llm._responder = GraphModel(
        classification={"intent": "document_question", "needs_tools": True},
        tool_calls=[[ToolCall(id="w1", name="save_note", arguments={"text": "RPO=5"})]],
        answers=[{"answer": "RPO is 5 minutes [S1]; saved [T1].", "citations": ["S1", "T1"]}],
    )
    ctx = ExecutionContext(
        tenant_id="tenant_a",
        limits=ExecutionLimits(),
        allowed_tools=services.tools.names,
        permissions=frozenset({"documents:read", "notes:write"}),
    )
    await services.executions.start(ctx, "graph", "Save the RPO")
    paused = await services.graph.run(ctx, "Save the RPO")
    await services.executions.save(ctx, paused)
    assert paused.status == "awaiting_approval" and note.saved == []
    row = await services.executions.get("tenant_a", ctx.execution_id)
    assert row.status == "awaiting_approval" and row.finished_at is None

    # "Restart": a new runner over the same Postgres checkpoint tables, context rebuilt from state.
    checkpointer = services.graph.graph.checkpointer
    fresh = GraphRunner(services.harness, checkpointer)  # type: ignore[arg-type]
    restored = await fresh.restore_context(ctx.execution_id)
    assert restored is not None and restored.counters.model_calls == ctx.counters.model_calls
    done = await fresh.resume(restored, approved=True)
    await services.executions.save(restored, done)

    assert done.status == "answered" and note.saved == ["RPO=5"]
    assert done.verification == "supported" and done.citations[0].filename == "dr.md"
    row = await services.executions.get("tenant_a", ctx.execution_id)
    assert row.status == "answered" and row.finished_at is not None
    events = await services.executions.events("tenant_a", ctx.execution_id)
    assert [e.seq for e in events] == list(range(len(events)))  # no gaps or duplicates
    assert any(e.kind == "approval" and e.status == "ok" for e in events)
