"""The LangGraph workflow (product.txt Section 7) and its runner.

    classify_request -> plan_workflow -> [retrieve_evidence] -> [select_or_execute_tools
    <-> human_approval] -> generate_answer -> validate_citations -> verify_answer
    -> finalize_execution, with retry_or_revise looping back to generate_answer (bounded).

When to use what:
- Answerer (app/rag/answer.py): one retrieval + one generation. Enough for plain lookups; the
  cheapest and most predictable path.
- AgentLoop: the model chooses tools turn by turn. Good when the number and order of tool calls
  is unknown up front; bounded by the harness, but its path is decided by the model.
- This graph: the path is fixed in code and the model fills in each step. Use it when steps
  must happen in a known order (verify before answering), when a run must pause for a human
  and resume later, or when each step needs its own model, prompt, and test.
"""

import logging
import uuid
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.agents import nodes
from app.agents.evidence import EvidenceStore, FinalAnswer
from app.agents.harness import Harness
from app.agents.result import AgentRunResult, RunStatus, build_result
from app.agents.state import AgentState, GraphDeps
from app.agents.tuning import AgentTuning
from app.core.errors import ConflictError
from app.core.execution_context import ExecutionContext

logger = logging.getLogger(__name__)
# Bump when the node set or routing changes (v2: workspace tool rounds, duplicate-call guard).
WORKFLOW_VERSION = "agent-graph-workflow-v2"


def build_graph(
    checkpointer: BaseCheckpointSaver[Any] | None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    g = StateGraph(AgentState, context_schema=GraphDeps)
    for fn in (
        nodes.classify_request,
        nodes.plan_workflow,
        nodes.retrieve_evidence,
        nodes.select_or_execute_tools,
        nodes.human_approval,
        nodes.generate_answer,
        nodes.validate_citations,
        nodes.verify_answer,
        nodes.retry_or_revise,
        nodes.finalize_execution,
    ):
        # LangGraph injects `runtime` by parameter name; its typing protocol is stricter than that.
        g.add_node(fn.__name__, fn)  # type: ignore[call-overload]
    g.add_edge(START, "classify_request")
    g.add_edge("classify_request", "plan_workflow")
    g.add_conditional_edges("plan_workflow", nodes.after_plan)
    g.add_conditional_edges("retrieve_evidence", nodes.after_retrieval)
    g.add_conditional_edges("select_or_execute_tools", nodes.after_tools)
    g.add_edge("human_approval", "select_or_execute_tools")
    g.add_conditional_edges("generate_answer", nodes.after_generate)
    g.add_conditional_edges("validate_citations", nodes.after_validation)
    g.add_conditional_edges("verify_answer", nodes.after_verification)
    g.add_conditional_edges("retry_or_revise", nodes.after_retry)
    g.add_edge("finalize_execution", END)
    return g.compile(checkpointer=checkpointer)


class GraphRunner:
    def __init__(
        self,
        harness: Harness,
        checkpointer: BaseCheckpointSaver[Any] | None,
        *,
        max_output_tokens: int = 1024,
        tuning: AgentTuning | None = None,
    ) -> None:
        self.harness = harness
        self.graph = build_graph(checkpointer)
        self._max_output_tokens = max_output_tokens
        self.tuning = tuning or AgentTuning()

    def _config(self, ctx: ExecutionContext) -> RunnableConfig:
        # Recursion limit is a backstop only; the harness step budget stops runs first.
        return {
            "configurable": {"thread_id": str(ctx.execution_id)},
            "recursion_limit": ctx.limits.max_graph_steps + 5,
        }

    def _deps(self, ctx: ExecutionContext) -> GraphDeps:
        return GraphDeps(self.harness, ctx, self._max_output_tokens, self.tuning)

    @staticmethod
    def for_graph(ctx: ExecutionContext) -> ExecutionContext:
        """Graph runs count nodes as steps, so they use the graph step limit."""
        ctx.limits = ctx.limits.model_copy(update={"max_steps": ctx.limits.max_graph_steps})
        return ctx

    async def run(self, ctx: ExecutionContext, question: str) -> AgentRunResult:
        self.for_graph(ctx)
        initial: AgentState = {
            "execution_id": str(ctx.execution_id),
            "request_id": ctx.request_id,
            "question": question,
        }
        return await self._invoke(ctx, initial)

    async def resume(
        self,
        ctx: ExecutionContext,
        *,
        approved: bool,
        reason: str | None = None,
        call_id: str | None = None,
    ) -> AgentRunResult:
        """Resume a paused run. Pass a context from restore_context(): the original in-memory
        one still counts the human's wait against the deadline. Callers must ensure a single
        resume per decision (AgentService claims the approval row first)."""
        state = await self.graph.aget_state(self._config(ctx))
        if not state.interrupts:
            raise ConflictError("Execution is not waiting for approval")
        decision = {"approved": approved, "reason": reason, "call_id": call_id}
        return await self._invoke(ctx, Command(resume=decision))

    async def _invoke(self, ctx: ExecutionContext, payload: Any) -> AgentRunResult:
        try:
            await self.graph.ainvoke(payload, self._config(ctx), context=self._deps(ctx))
        except GraphRecursionError:
            ctx.record("final", "graph", "error", error_code="budget_exhausted")
            return build_result(
                ctx, "graph", EvidenceStore(), "budget_exhausted", error_code="budget_exhausted"
            )
        return await self.result(ctx)

    async def result(self, ctx: ExecutionContext) -> AgentRunResult:
        snapshot = await self.graph.aget_state(self._config(ctx))
        state = cast(AgentState, snapshot.values)
        store = EvidenceStore.from_state(state.get("evidence"))
        draft = FinalAnswer.model_validate(state["draft"]) if state.get("draft") else None
        status = cast(
            RunStatus,
            "awaiting_approval" if snapshot.interrupts else state.get("status") or "failed",
        )
        return build_result(
            ctx,
            "graph",
            store,
            status,
            final=draft,
            error_code=state.get("error_code"),
            verification=state.get("verification"),
        )

    async def pending_approval(self, ctx: ExecutionContext) -> dict[str, Any] | None:
        snapshot = await self.graph.aget_state(self._config(ctx))
        return snapshot.interrupts[0].value if snapshot.interrupts else None

    async def restore_context(self, execution_id: uuid.UUID) -> ExecutionContext | None:
        """Rebuild the ExecutionContext of a paused run from its last checkpoint."""
        snapshot = await self.graph.aget_state(
            RunnableConfig(configurable={"thread_id": str(execution_id)})
        )
        budget = snapshot.values.get("budget") if snapshot.values else None
        return ExecutionContext.restore(budget) if budget else None
