"""LangGraph state and runtime dependencies.

State is what the checkpointer persists, so it holds only JSON-safe data: no clients, no secrets,
no model reasoning. Live objects (the harness, the ExecutionContext) travel in GraphDeps, passed
per invocation through LangGraph's runtime context and rebuilt from `budget` on resume.
"""

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from app.agents.harness import Harness
from app.core.execution_context import ExecutionContext

Intent = Literal["document_question", "calculation", "metadata_lookup", "other"]
CallStatus = Literal["queued", "pending", "approved", "rejected"]


class PendingCall(TypedDict):
    call: dict[str, Any]  # ToolCall.model_dump()
    status: CallStatus
    reason: str | None


class AgentState(TypedDict, total=False):
    execution_id: str
    request_id: str
    question: str
    intent: Intent
    needs_retrieval: bool
    needs_tools: bool
    plan: list[str]  # search queries
    evidence: dict[str, Any]  # EvidenceStore.to_state()
    pending_calls: list[PendingCall]
    draft: dict[str, Any] | None  # FinalAnswer
    previous_output: str  # raw last answer output, replayed as context for a correction
    findings: list[str]  # validation / verification problems for the current draft
    feedback: list[str]  # what the next generate_answer attempt must fix
    verification: str | None
    status: str | None  # set once the outcome is decided
    error_code: str | None
    budget: dict[str, Any]  # ExecutionContext.snapshot()


@dataclass
class GraphDeps:
    harness: Harness
    ctx: ExecutionContext
    max_output_tokens: int = 1024
