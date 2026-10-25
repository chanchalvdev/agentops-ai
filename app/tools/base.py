"""Common tool interface.

A tool declares its contract (schemas, permissions, timeout, idempotency, approval) as data. The
model may *request* a tool; only the harness validates, authorizes, and executes it.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

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

    def map_error(self, exc: Exception) -> AppError:
        """Translate an unexpected exception into a typed error safe to show the model."""
        if isinstance(exc, AppError):
            return exc
        return ToolExecutionError(f"{self.name} failed", detail=repr(exc))

    def spec(self) -> ToolSpec:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(name=self.name, description=self.description, parameters=schema)
