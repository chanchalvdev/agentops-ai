"""Common tool interface.

A tool declares its contract (schemas, permissions, timeout, idempotency, approval) as data. The
model may *request* a tool; only the harness validates, authorizes, and executes it.
"""

from abc import ABC, abstractmethod
from collections.abc import Hashable
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

from app.core.errors import AppError, ToolExecutionError
from app.llm.schemas import ToolSpec

if TYPE_CHECKING:
    from app.core.execution_context import ExecutionContext


class Tool[I: BaseModel, O: BaseModel](ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    input_model: type[I]
    output_model: type[O]
    permissions: ClassVar[frozenset[str]] = frozenset()
    timeout_s: ClassVar[float] = 10.0
    idempotent: ClassVar[bool] = True  # only idempotent tools are retried on transient errors
    requires_approval: ClassVar[bool] = False  # write/exec tools pause for a human decision

    @abstractmethod
    async def run(self, args: I, ctx: "ExecutionContext") -> O: ...

    def approval_needed(self, args: I, ctx: "ExecutionContext") -> bool:
        """Per-call approval decision. `requires_approval` means "may need approval" (the custom
        loop, which cannot pause, drops such tools); a tool can relax it for safe arguments.
        Raising an AppError here rejects the call before anyone is asked to approve it."""
        return self.requires_approval

    async def cache_key(self, args: I, ctx: "ExecutionContext") -> Hashable | None:
        """What the result depends on besides tenant, permissions, and arguments (the harness
        adds those), e.g. the corpus version. None = never cache. Only idempotent tools that need
        no approval are ever looked up."""
        return None

    def ledger_arguments(self, args: I) -> dict[str, Any]:
        """Arguments as stored in the ledger and traces. Override to drop bulky or sensitive
        fields (e.g. file contents); the human reviewer still sees the full call."""
        return args.model_dump(mode="json")

    def ledger_output(self, output: O) -> dict[str, Any]:
        """A small, safe summary of the result for the ledger (IDs, counts), not the payload."""
        return {}

    def map_error(self, exc: Exception) -> AppError:
        """Translate an unexpected exception into a typed error safe to show the model."""
        if isinstance(exc, AppError):
            return exc
        return ToolExecutionError(f"{self.name} failed", detail=repr(exc))

    def spec(self) -> ToolSpec:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(name=self.name, description=self.description, parameters=schema)
