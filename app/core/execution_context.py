"""Typed state for one agent execution: identity, limits, counters, permissions, and the ledger.

The context is created per request and owned by the harness. Agents and graph nodes read it but
change it only through the harness, so every limit is enforced in one place.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.budgets import BudgetCounters, ExecutionLimits

EventKind = Literal["step", "model_call", "tool_call", "correction", "policy", "approval", "final"]
EventStatus = Literal["ok", "error", "rejected", "pending"]

DEFAULT_PERMISSIONS = frozenset({"documents:read"})


class LedgerEvent(BaseModel):
    """One attempted operation: decisions, references, and validated tool arguments (user-scoped
    data such as a search query, never secrets). Raw prompts and model reasoning are not stored."""

    seq: int
    kind: EventKind
    name: str
    status: EventStatus
    error_code: str | None = None
    message: str | None = None
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@dataclass
class ExecutionContext:
    tenant_id: str
    limits: ExecutionLimits
    allowed_tools: frozenset[str]
    permissions: frozenset[str] = DEFAULT_PERMISSIONS
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    execution_id: uuid.UUID = field(default_factory=uuid.uuid4)
    clock: Callable[[], float] = time.monotonic
    workflow_state: str = "created"
    counters: BudgetCounters = field(default_factory=BudgetCounters)
    ledger: list[LedgerEvent] = field(default_factory=list)
    trace_ids: list[str] = field(default_factory=list)
    approved_call_ids: set[str] = field(default_factory=set)
    cancel_reason: str | None = None
    started_at: float = field(init=False)

    def __post_init__(self) -> None:
        self.started_at = self.clock()

    @property
    def deadline_at(self) -> float:
        return self.started_at + self.limits.deadline_s

    def remaining_s(self) -> float:
        return self.deadline_at - self.clock()

    @property
    def cancelled(self) -> bool:
        return self.cancel_reason is not None

    def cancel(self, reason: str = "cancelled by caller") -> None:
        self.cancel_reason = reason

    def record(self, kind: EventKind, name: str, status: EventStatus, **fields: Any) -> LedgerEvent:
        event = LedgerEvent(seq=len(self.ledger), kind=kind, name=name, status=status, **fields)
        self.ledger.append(event)
        return event
