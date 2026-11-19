"""The agent and the workspace: writes wait for a human, rejected writes never happen, read-only
commands run directly, disallowed ones are refused before anyone is asked, and tenants without
workspace permission never see the tools."""

from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.custom_loop import AgentLoop
from app.agents.graph import GraphRunner
from app.agents.harness import Harness
from app.agents.service import AgentService
from app.core.budgets import ExecutionLimits
from app.core.execution_context import ExecutionContext
from app.core.security import DOCUMENT_PERMISSIONS, WORKSPACE_PERMISSIONS
from app.llm.provider import FakeProvider, LLMRouter, Route
from app.llm.schemas import LLMRequest, LLMResponse, ToolCall
from app.tools.calculator import Calculator
from app.tools.registry import ToolRegistry
from app.tools.workspace import workspace_tools
from app.workspace.audit import MemoryAudit
from app.workspace.jail import WorkspaceManager
from tests.unit.test_agent_graph import GraphModel
from tests.unit.test_agent_loop import FakeSearch, final, tool_call

WORKSPACE_TASK = {"intent": "workspace_task", "needs_tools": True}
WS_TOOLS = frozenset(
    {
        "workspace_list",
        "workspace_read",
        "workspace_search",
        "workspace_write",
        "workspace_edit",
        "workspace_exec",
    }
)


@pytest.fixture
def manager(tmp_path: Path) -> WorkspaceManager:
    m = WorkspaceManager(tmp_path / "ws", MemoryAudit(), exec_timeout_s=20)
    (m.for_tenant("tenant_a").root / "notes.md").write_text("RPO is 5 minutes.\n")
    return m


def registry(manager: WorkspaceManager) -> ToolRegistry:
    return ToolRegistry([FakeSearch(), Calculator(), *workspace_tools(manager)])


def runner(model: GraphModel, manager: WorkspaceManager) -> GraphRunner:
    provider = FakeProvider(model)
    router = LLMRouter(
        {"main": [Route(provider, "main")], "small": [Route(provider, "small")]}, max_retries=0
    )
    return GraphRunner(Harness(router, registry(manager)), InMemorySaver())


def ctx(permissions: frozenset[str] = DOCUMENT_PERMISSIONS | WORKSPACE_PERMISSIONS) -> Any:
    return ExecutionContext(
        tenant_id="tenant_a",
        limits=ExecutionLimits(deadline_s=30),
        allowed_tools=WS_TOOLS | {"document_search", "calculator"},
        permissions=permissions,
    )


def call(name: str, call_id: str, **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]


def notes(manager: WorkspaceManager) -> str:
    return (manager.for_tenant("tenant_a").root / "notes.md").read_text()


async def test_edit_waits_for_approval_then_applies(manager: WorkspaceManager) -> None:
    model = GraphModel(
        classification=WORKSPACE_TASK,
        tool_calls=[
            call("workspace_read", "r1", path="notes.md"),
            call("workspace_edit", "e1", path="notes.md", old="5 minutes", new="300 seconds"),
            [],  # after the edit: nothing more to do
        ],
        answers=[{"answer": "Changed the RPO to 300 seconds [T2].", "citations": ["T2"]}],
    )
    graph = runner(model, manager)
    c = ctx()
    paused = await graph.run(c, "Change the RPO in notes.md to seconds")
    assert paused.status == "awaiting_approval"
    assert notes(manager) == "RPO is 5 minutes.\n"  # nothing written yet
    pending = await graph.pending_approval(c)
    assert pending is not None and pending["tool"] == "workspace_edit"
    assert pending["arguments"]["new"] == "300 seconds"  # the reviewer sees the full call
    assert model.calls == ["classify", "tools", "tools"]  # read, then propose the edit

    resumed_ctx = await graph.restore_context(c.execution_id)
    assert resumed_ctx is not None
    done = await graph.resume(resumed_ctx, approved=True, call_id=pending["call_id"])
    assert done.status == "answered", done
    assert notes(manager) == "RPO is 300 seconds.\n"
    edit = [e for e in resumed_ctx.ledger if e.kind == "tool_call" and e.name == "workspace_edit"]
    assert edit[-1].status == "ok"
    assert "old" not in edit[-1].data["arguments"]  # ledger keeps sizes and hashes, not text


async def test_rejected_write_is_never_performed(manager: WorkspaceManager) -> None:
    model = GraphModel(
        classification=WORKSPACE_TASK,
        tool_calls=[call("workspace_write", "w1", path="notes.md", content="wiped")],
        answers=[{"answer": "The change was rejected [T1].", "citations": ["T1"]}],
    )
    graph = runner(model, manager)
    c = ctx()
    assert (await graph.run(c, "Overwrite notes.md")).status == "awaiting_approval"
    restored = await graph.restore_context(c.execution_id)
    assert restored is not None
    result = await graph.resume(restored, approved=False, reason="no", call_id="w1")
    assert result.status == "answered"
    assert notes(manager) == "RPO is 5 minutes.\n"
    assert not any(e.kind == "tool_call" and e.status == "ok" for e in restored.ledger)
    assert result.tool_references[0].output["rejected_by_reviewer"] is True


async def test_read_only_command_runs_without_approval(manager: WorkspaceManager) -> None:
    model = GraphModel(
        classification=WORKSPACE_TASK,
        tool_calls=[call("workspace_exec", "x1", argv=["grep", "-rn", "RPO", "."]), []],
        answers=[{"answer": "notes.md line 1 mentions the RPO [T1].", "citations": ["T1"]}],
    )
    result = await runner(model, manager).run(ctx(), "Where is the RPO mentioned?")
    assert result.status == "answered"
    assert "notes.md:1:RPO" in result.tool_references[0].output["output"]


async def test_exec_that_runs_code_needs_approval(manager: WorkspaceManager) -> None:
    model = GraphModel(
        classification=WORKSPACE_TASK,
        tool_calls=[call("workspace_exec", "p1", argv=["python", "-m", "pytest", "-q"])],
    )
    graph = runner(model, manager)
    c = ctx()
    assert (await graph.run(c, "Run the tests")).status == "awaiting_approval"


async def test_disallowed_command_is_rejected_before_any_approval(
    manager: WorkspaceManager,
) -> None:
    model = GraphModel(
        classification=WORKSPACE_TASK,
        tool_calls=[call("workspace_exec", "b1", argv=["bash", "-c", "rm -rf ~"]), []],
        answers=[{"answer": "That command is not allowed [T1].", "citations": ["T1"]}],
    )
    c = ctx()
    result = await runner(model, manager).run(c, "Clean up")
    assert result.status == "answered"
    assert not any(e.kind == "approval" for e in c.ledger)
    rejected = [e for e in c.ledger if e.kind == "tool_call" and e.status == "rejected"]
    assert rejected[0].error_code == "command_not_allowed"


async def test_rereading_a_file_is_allowed(manager: WorkspaceManager) -> None:
    """Recorder finding: the duplicate guard refused identical reads, so the model could not
    re-read a file to check its own edit. Only calls with side effects are guarded."""
    model = GraphModel(
        classification=WORKSPACE_TASK,
        tool_calls=[call("workspace_read", "a", path="notes.md")] * 2 + [[]],
        answers=[{"answer": "RPO is 5 minutes [T2].", "citations": ["T2"]}],
    )
    c = ctx()
    await runner(model, manager).run(c, "Read notes.md twice")
    reads = [e for e in c.ledger if e.kind == "tool_call" and e.name == "workspace_read"]
    assert [e.status for e in reads] == ["ok", "ok"]


async def test_workspace_rounds_are_bounded(manager: WorkspaceManager) -> None:
    # Different (missing) paths: each round "does something", so only the round cap stops it.
    reads = [call("workspace_read", f"r{i}", path=f"missing{i}.md") for i in range(10)]
    model = GraphModel(classification=WORKSPACE_TASK, tool_calls=reads)
    model.answers = [{"answer": "None of the files exist [T1].", "citations": ["T1"]}]
    result = await runner(model, manager).run(ctx(), "Read the missing files")
    assert result.status == "answered"
    assert model.calls.count("tools") == 4  # nodes.MAX_TOOL_ROUNDS


async def test_tenant_without_workspace_permission(manager: WorkspaceManager) -> None:
    tools = registry(manager)
    service = AgentService(
        AgentLoop(Harness(None, tools)),  # type: ignore[arg-type]
        lambda: None,  # type: ignore[arg-type, return-value]
        tools,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        ExecutionLimits(),
    )
    plain = service.new_context("tenant_b", "r1")
    assert not plain.allowed_tools & WS_TOOLS  # not even offered to the model
    granted = service.new_context(
        "tenant_a", "r2", permissions=DOCUMENT_PERMISSIONS | WORKSPACE_PERMISSIONS
    )
    assert granted.allowed_tools >= WS_TOOLS

    model = GraphModel(
        classification=WORKSPACE_TASK,
        tool_calls=[call("workspace_read", "r1", path="notes.md"), []],
        answers=[{"answer": "Could not read it [T1].", "citations": ["T1"]}],
    )
    c = ctx(permissions=DOCUMENT_PERMISSIONS)  # explicitly offered, still refused when called
    await runner(model, manager).run(c, "Read notes.md")
    rejected = [e for e in c.ledger if e.kind == "tool_call" and e.status == "rejected"]
    assert rejected[0].error_code == "tool_not_permitted"


async def test_loop_offers_only_read_only_workspace_tools(manager: WorkspaceManager) -> None:
    seen: list[set[str]] = []

    def respond(request: LLMRequest) -> LLMResponse:
        seen.append({t.name for t in request.tools})
        calls = (
            [tool_call("workspace_read", "r1", path="notes.md")]
            if len(seen) == 1
            else [final("RPO is 5 minutes [T1].", ["T1"])]
        )
        return LLMResponse(
            content="", tool_calls=calls, model=request.model, provider="fake", latency_ms=1
        )

    provider = FakeProvider(respond)
    router = LLMRouter(
        {"main": [Route(provider, "m")], "small": [Route(provider, "s")]}, max_retries=0
    )
    result = await AgentLoop(Harness(router, registry(manager))).run(ctx(), "RPO in notes.md?")
    assert result.status == "answered"
    assert seen[0] & WS_TOOLS == {"workspace_list", "workspace_read", "workspace_search"}


async def test_identical_write_after_success_is_refused_without_approval(
    manager: WorkspaceManager,
) -> None:
    """Live failure: after an approved edit, qwen3:8b proposed the same edit again. Approving it
    would have applied it twice (the `old` text still occurred once)."""
    edit = {"path": "notes.md", "old": "5 minutes.", "new": "5 minutes.\nRTO is 15 minutes."}
    model = GraphModel(
        classification=WORKSPACE_TASK,
        tool_calls=[call("workspace_edit", "e1", **edit), call("workspace_edit", "e2", **edit)],
        answers=[{"answer": "Added the RTO [T1].", "citations": ["T1"]}],
    )
    graph = runner(model, manager)
    c = ctx()
    await graph.run(c, "Add the RTO to notes.md")
    restored = await graph.restore_context(c.execution_id)
    assert restored is not None
    result = await graph.resume(restored, approved=True, call_id="e1")
    assert result.status == "answered"
    assert notes(manager).count("RTO is 15 minutes.") == 1
    assert [e.error_code for e in restored.ledger if e.status == "rejected"] == ["duplicate_call"]
    pending = [e for e in restored.ledger if e.kind == "approval" and e.status == "pending"]
    assert [e.data["call_id"] for e in pending] == ["e1"]  # no approval was asked for e2


async def test_rerunning_tests_after_an_edit_is_allowed(manager: WorkspaceManager) -> None:
    """Recorder finding: the guard must not refuse "run tests, edit, run tests again"."""
    (manager.for_tenant("tenant_a").root / "test_n.py").write_text("def test_n():\n    pass\n")
    pytest_call = {"argv": ["python", "-m", "pytest", "-q", "test_n.py"]}
    edit = {"path": "notes.md", "old": "5 minutes", "new": "6 minutes"}
    model = GraphModel(
        classification=WORKSPACE_TASK,
        tool_calls=[
            call("workspace_exec", "t1", **pytest_call),
            call("workspace_edit", "e1", **edit),
            call("workspace_exec", "t2", **pytest_call),
            call("workspace_exec", "t3", **pytest_call),  # immediate identical repeat: refused
        ],
        answers=[{"answer": "Done [T1].", "citations": ["T1"]}],
    )
    graph = runner(model, manager)
    c = ctx()
    result = await graph.run(c, "test, edit, test")
    for call_id in ("t1", "e1", "t2"):
        assert result.status == "awaiting_approval", call_id
        restored = await graph.restore_context(c.execution_id)
        assert restored is not None
        result = await graph.resume(restored, approved=True, call_id=call_id)
        c = restored
    ran = [e.data["call_id"] for e in c.ledger if e.kind == "tool_call" and e.status == "ok"]
    assert ran == ["t1", "e1", "t2"]
    assert [e.data["call_id"] for e in c.ledger if e.error_code == "duplicate_call"] == ["t3"]


async def test_workspace_report_is_never_insufficient_evidence(manager: WorkspaceManager) -> None:
    """Live eval: the model flagged reports of rejected actions as insufficient_evidence."""
    model = GraphModel(
        classification=WORKSPACE_TASK,
        tool_calls=[call("workspace_write", "w1", path="x.md", content="x")],
        answers=[
            {
                "answer": "The write was rejected [T1].",
                "citations": ["T1"],
                "insufficient_evidence": True,
            }
        ],
    )
    graph = runner(model, manager)
    c = ctx()
    await graph.run(c, "Create x.md")
    restored = await graph.restore_context(c.execution_id)
    assert restored is not None
    result = await graph.resume(restored, approved=False, call_id="w1")
    assert result.status == "answered" and result.verification == "supported"


def test_workspace_outcome_states_that_a_retried_edit_was_made() -> None:
    from app.agents.evidence import EvidenceStore
    from app.agents.nodes import workspace_outcome

    store = EvidenceStore()
    store.add_tool_result("workspace_edit", {"error": "invalid_request", "message": "not found"})
    store.add_tool_result("workspace_read", {"path": "calc/mathlib.py", "content": "..."})
    store.add_tool_result(
        "workspace_edit",
        {
            "outcome": "applied",
            "path": "calc/mathlib.py",
            "bytes": 9,
            "sha256": "x",
            "created": False,
        },
    )
    text = workspace_outcome(store)
    assert "calc/mathlib.py: changed (applied) [T3]" in text
    assert "[T1] (invalid_request) failed" in text and "WAS made" in text

    only_failed = EvidenceStore()
    only_failed.add_tool_result("workspace_write", {"error": "workspace_access_denied"})
    assert "No change was made" in workspace_outcome(only_failed)
    assert workspace_outcome(EvidenceStore()) == ""


async def test_old_write_receipts_are_pruned(tmp_path: Path) -> None:
    import os
    import time
    import uuid

    from app.workspace import jail
    from app.workspace.jail import Actor

    m = WorkspaceManager(tmp_path / "ws", MemoryAudit(), receipt_ttl_s=3600)
    ws = m.for_tenant("tenant_a")
    receipts = tmp_path / "ws" / ".receipts" / "tenant_a"
    receipts.mkdir(parents=True)
    stale, fresh = receipts / "old.json", receipts / "new.json"
    stale.write_text("{}")
    fresh.write_text("{}")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    jail._LAST_PRUNE.pop(receipts, None)
    agent = Actor(kind="agent", execution_id=uuid.uuid4(), call_id="c1")
    await ws.write("a.txt", "x", agent)
    assert not stale.exists() and fresh.exists()
    assert len(list(receipts.iterdir())) == 2  # the fresh one and this write's own receipt
