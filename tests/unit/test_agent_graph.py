"""LangGraph workflow on a fake model: routing, bounded revision, verification, and
human-in-the-loop interrupt/resume with an in-memory checkpointer."""

import json
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from app.agents import nodes
from app.agents.graph import GraphRunner
from app.agents.harness import Harness
from app.core.budgets import ExecutionLimits
from app.core.errors import ConflictError
from app.core.execution_context import ExecutionContext
from app.llm.provider import FakeProvider, LLMRouter, Route
from app.llm.schemas import LLMRequest, LLMResponse, ToolCall
from app.tools.base import Tool
from app.tools.calculator import Calculator
from app.tools.registry import ToolRegistry
from tests.unit.test_agent_loop import CHUNK_A, FakeSearch


class NoteIn(BaseModel):
    text: str


class NoteOut(BaseModel):
    saved: bool


class SaveNote(Tool[NoteIn, NoteOut]):
    """A write tool: must pause for a human before it runs."""

    name = "save_note"
    description = "Save a note."
    input_model = NoteIn
    output_model = NoteOut
    permissions: ClassVar[frozenset[str]] = frozenset({"notes:write"})
    idempotent = False
    requires_approval = True

    def __init__(self) -> None:
        self.saved: list[str] = []

    async def run(self, args: NoteIn, ctx: ExecutionContext) -> NoteOut:
        self.saved.append(args.text)
        return NoteOut(saved=True)


@dataclass
class GraphModel:
    """Answers each node by its system prompt. Lists are consumed one reply per call."""

    classification: Any = field(
        default_factory=lambda: {"intent": "document_question", "needs_tools": False}
    )
    plan: Any = field(default_factory=lambda: {"search_queries": ["RPO"]})
    tool_calls: list[list[ToolCall]] = field(default_factory=list)
    answers: list[Any] = field(
        default_factory=lambda: [{"answer": "RPO is 5 min [S1].", "citations": ["S1"]}]
    )
    verdicts: list[Any] = field(default_factory=lambda: [{"supported": True}])
    calls: list[str] = field(default_factory=list)

    def __call__(self, request: LLMRequest) -> LLMResponse:
        system = request.messages[0].content
        calls: list[ToolCall] = []
        if system == nodes.CLASSIFY_PROMPT:
            self.calls.append("classify")
            content = self.classification
        elif system == nodes.PLAN_PROMPT:
            self.calls.append("plan")
            content = self.plan
        elif system == nodes.TOOLS_PROMPT:
            self.calls.append("tools")
            calls, content = (self.tool_calls.pop(0) if self.tool_calls else []), ""
        elif system == nodes.ANSWER_PROMPT:
            self.calls.append("answer")
            content = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            if isinstance(content, dict):
                content = {"insufficient_evidence": False, **content}
        elif system == nodes.VERIFY_PROMPT:
            self.calls.append("verify")
            content = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
        else:
            raise AssertionError(f"unexpected prompt: {system[:40]}")
        if not isinstance(content, str):
            content = json.dumps(content)
        return LLMResponse(
            content=content, tool_calls=calls, model=request.model, provider="fake", latency_ms=1
        )


def make_runner(
    model: GraphModel, search: FakeSearch | None = None, saver: InMemorySaver | None = None
) -> tuple[GraphRunner, SaveNote, InMemorySaver]:
    provider = FakeProvider(model)
    router = LLMRouter(
        {"main": [Route(provider, "main")], "small": [Route(provider, "small")]}, max_retries=0
    )
    note = SaveNote()
    registry = ToolRegistry([search or FakeSearch(), Calculator(), note])
    saver = saver or InMemorySaver()
    return GraphRunner(Harness(router, registry), saver), note, saver


def make_ctx(**limits: Any) -> ExecutionContext:
    return ExecutionContext(
        tenant_id="tenant_a",
        limits=ExecutionLimits.model_validate({"deadline_s": 30, **limits}),
        allowed_tools=frozenset({"document_search", "calculator", "save_note"}),
        permissions=frozenset({"documents:read", "notes:write"}),
    )


def nodes_run(ctx: ExecutionContext) -> list[str]:
    return [e.name for e in ctx.ledger if e.kind == "step" and e.status == "ok"]


async def test_document_question_happy_path() -> None:
    model = GraphModel()
    runner, _, _ = make_runner(model)
    ctx = make_ctx()
    result = await runner.run(ctx, "What is the RPO?")
    assert result.status == "answered" and result.verification == "supported"
    assert [c.chunk_id for c in result.citations] == [str(CHUNK_A)]
    assert nodes_run(ctx) == [
        "classify_request",
        "plan_workflow",
        "retrieve_evidence",
        "generate_answer",
        "validate_citations",
        "verify_answer",
    ]
    assert ctx.ledger[-1].kind == "final"
    assert model.calls == ["classify", "plan", "answer", "verify"]


async def test_original_question_is_always_searched() -> None:
    search = FakeSearch()
    runner, _, _ = make_runner(GraphModel(plan={"search_queries": ["rewritten"]}), search)
    await runner.run(make_ctx(), "What is the RPO?")
    assert search.calls == 2  # the question itself + the rewrite


async def test_calculation_skips_retrieval_and_cites_tool_result() -> None:
    model = GraphModel(
        classification={"intent": "calculation", "needs_tools": True},
        tool_calls=[[ToolCall(id="c", name="calculator", arguments={"expression": "15*60"})]],
        answers=[{"answer": "900 seconds [T1].", "citations": ["T1"]}],
    )
    search = FakeSearch()
    runner, _, _ = make_runner(model, search)
    ctx = make_ctx()
    result = await runner.run(ctx, "15 minutes in seconds?")
    assert result.status == "answered" and search.calls == 0
    assert "retrieve_evidence" not in nodes_run(ctx)
    assert result.tool_references[0].output["result"] == 900


async def test_unknown_citation_is_revised_once() -> None:
    model = GraphModel(
        answers=[
            {"answer": "5 min [S7].", "citations": ["S7"]},
            {"answer": "5 min [S1].", "citations": ["S1"]},
        ]
    )
    runner, _, _ = make_runner(model)
    ctx = make_ctx()
    result = await runner.run(ctx, "?")
    assert result.status == "answered" and result.corrections == {"citations": 1}
    assert nodes_run(ctx).count("generate_answer") == 2
    assert "retry_or_revise" in nodes_run(ctx)


async def test_unsupported_answer_ends_in_failed_validation_after_bounded_revisions() -> None:
    model = GraphModel(verdicts=[{"supported": False, "unsupported_claims": ["RPO is 1 min"]}])
    runner, _, _ = make_runner(model)
    ctx = make_ctx(max_corrections=2)
    result = await runner.run(ctx, "?")
    assert result.status == "failed_validation"
    assert model.calls.count("answer") == 3 and result.corrections == {"verification": 2}
    assert "5 min" not in result.answer  # the unverified draft is not returned as an answer


async def test_verifier_garbage_is_reported_not_treated_as_passed() -> None:
    runner, _, _ = make_runner(GraphModel(verdicts=["not json"]))
    result = await runner.run(make_ctx(), "?")
    assert result.status == "answered" and result.verification == "unavailable"


async def test_no_evidence_answers_insufficient_without_calling_the_answer_model() -> None:
    model = GraphModel()
    runner, _, _ = make_runner(model, FakeSearch(passages=[]))
    result = await runner.run(make_ctx(), "CEO's favourite colour?")
    assert result.status == "insufficient_evidence"
    assert "answer" not in model.calls and "verify" not in model.calls


async def test_invalid_classifier_output_falls_back_to_retrieval() -> None:
    search = FakeSearch()
    runner, _, _ = make_runner(GraphModel(classification="garbage"), search)
    result = await runner.run(make_ctx(), "?")
    assert result.status == "answered" and search.calls >= 1


async def test_graph_step_budget_stops_the_run() -> None:
    runner, _, _ = make_runner(GraphModel())
    ctx = make_ctx(max_graph_steps=3)
    result = await runner.run(ctx, "?")
    assert result.status == "budget_exhausted" and result.error_code == "budget_exhausted"
    assert nodes_run(ctx) == ["classify_request", "plan_workflow", "retrieve_evidence"]
    assert ctx.ledger[-1].kind == "final" and ctx.ledger[-1].data["status"] == "budget_exhausted"


async def test_cancellation_mid_graph() -> None:
    model = GraphModel()
    ctx = make_ctx()
    original = model.__call__

    def cancel_on_plan(request: LLMRequest) -> LLMResponse:
        if request.messages[0].content == nodes.PLAN_PROMPT:
            ctx.cancel("stop")
        return original(request)

    runner, _, _ = make_runner(model)
    runner.harness.llm._routes["main"][0].provider._responder = cancel_on_plan  # type: ignore[attr-defined]
    result = await runner.run(ctx, "?")
    assert result.status == "cancelled" and "answer" not in model.calls


# --- human-in-the-loop ------------------------------------------------------------------------


def write_model() -> GraphModel:
    return GraphModel(
        classification={"intent": "document_question", "needs_tools": True},
        tool_calls=[[ToolCall(id="w1", name="save_note", arguments={"text": "RPO=5"})]],
        answers=[{"answer": "Saved; RPO is 5 min [S1] [T1].", "citations": ["S1", "T1"]}],
    )


async def test_write_tool_pauses_and_runs_only_after_approval() -> None:
    runner, note, _ = make_runner(write_model())
    ctx = make_ctx()
    paused = await runner.run(ctx, "Save the RPO as a note")
    assert paused.status == "awaiting_approval" and note.saved == []
    assert await runner.pending_approval(ctx) == {
        "call_id": "w1",
        "tool": "save_note",
        "arguments": {"text": "RPO=5"},
    }

    done = await runner.resume(ctx, approved=True)
    assert done.status == "answered" and note.saved == ["RPO=5"]
    assert any(e.kind == "approval" and e.status == "ok" for e in ctx.ledger)


async def test_rejected_approval_performs_no_write() -> None:
    model = write_model()
    model.answers = [{"answer": "The note was not saved [T1].", "citations": ["T1"]}]
    runner, note, _ = make_runner(model)
    ctx = make_ctx()
    await runner.run(ctx, "Save the RPO as a note")
    done = await runner.resume(ctx, approved=False, reason="not now")
    assert note.saved == [] and done.status == "answered"
    assert done.tool_references[0].output == {"rejected_by_reviewer": True, "reason": "not now"}


async def test_resume_in_a_new_runner_restores_budgets_from_the_checkpoint() -> None:
    model = write_model()
    runner, _, saver = make_runner(model)
    ctx = make_ctx(max_model_calls=6)
    await runner.run(ctx, "Save the RPO as a note")
    used = ctx.counters.model_calls

    # Simulate a new process: fresh runner, same checkpoint store, context rebuilt from state.
    runner2, note2, _ = make_runner(model, saver=saver)
    restored = await runner2.restore_context(ctx.execution_id)
    assert restored is not None and restored.counters.model_calls == used
    assert restored.limits.max_steps == ctx.limits.max_graph_steps
    done = await runner2.resume(restored, approved=True)
    assert done.status == "answered" and note2.saved == ["RPO=5"]
    assert done.model_calls == used + 2  # answer + verify, counted on top of the restored total


async def test_resume_without_a_pending_approval_is_a_conflict() -> None:
    runner, _, _ = make_runner(GraphModel())
    ctx = make_ctx()
    await runner.run(ctx, "?")
    with pytest.raises(ConflictError):
        await runner.resume(ctx, approved=True)


async def test_write_tool_without_permission_never_reaches_approval() -> None:
    runner, note, _ = make_runner(write_model())
    ctx = make_ctx()
    ctx.permissions = frozenset({"documents:read"})
    result = await runner.run(ctx, "Save the RPO as a note")
    assert result.status != "awaiting_approval" and note.saved == []


# --- regressions from the M4 milestone review ---------------------------------------------------


async def test_retrieval_outage_is_a_failure_not_insufficient_evidence() -> None:
    from app.core.errors import RetrievalError

    runner, _, _ = make_runner(GraphModel(), FakeSearch(error=RetrievalError("down")))
    result = await runner.run(make_ctx(), "?")
    assert result.status == "failed" and result.error_code == "retrieval_error"


async def test_decision_for_a_different_call_is_not_an_approval() -> None:
    runner, note, _ = make_runner(write_model())
    ctx = make_ctx()
    await runner.run(ctx, "Save the RPO as a note")
    done = await runner.resume(ctx, approved=True, call_id="some-other-call")
    assert note.saved == [] and done.tool_references[0].output["rejected_by_reviewer"] is True


async def test_unparseable_answer_uses_invalid_output_corrections() -> None:
    model = GraphModel(answers=["not json", {"answer": "5 min [S1].", "citations": ["S1"]}])
    runner, _, _ = make_runner(model)
    result = await runner.run(make_ctx(), "?")
    assert result.status == "answered" and result.corrections == {"invalid_output": 1}
